"""Physical cleanup and retained export on disposable PostgreSQL."""
import json

import psycopg
import pytest

from test_lifecycle import fixture, seed, save, store
from cart_recovery.domain.privacy import purge_withdrawn, export_retained_page


def test_withdrawn_source_is_physically_removed(store):
    f = fixture('inferred_preference')
    seed(store, f)
    save(store, f)
    scope = f['bindings']['scope']
    store.withdraw_source(scope, f['payload']['objects'][0]['sources'][0])
    purge_withdrawn(store, scope)
    exported = store.export_subject(scope)
    assert exported['sources'] == []
    assert exported['payload'] is None


def test_cleanup_preserves_unrelated_memory_and_fences_reuse(store):
    from copy import deepcopy
    from cart_recovery.domain.knowledge import KnowledgeError
    from cart_recovery.domain.store import StoreConflict
    inferred, explicit = fixture('inferred_preference'), fixture()
    seed(store, inferred)
    seed(store, explicit)
    combined = deepcopy(inferred)
    combined['payload']['objects'] += explicit['payload']['objects']
    save(store, combined)
    scope = combined['bindings']['scope']
    store.withdraw_source(scope, inferred['payload']['objects'][0]['sources'][0])
    result = purge_withdrawn(store, scope)
    assert result == dict(version=3, removed_sources=1, removed_objects=2, whole_application_complete=False)
    exported = store.export_subject(scope)
    assert exported['payload']['objects'] == explicit['payload']['objects']
    assert exported['payload']['links'] == []
    assert len(exported['sources']) == 1
    assert len(exported['commands']) == 1
    assert purge_withdrawn(store, scope)['version'] == 3
    assert save(store, explicit, 3, 'unrelated')['version'] == 4
    for field in ('object_id', 'assertion_id'):
        attempted = deepcopy(explicit)
        attempted['payload']['objects'][0][field] = inferred['payload']['objects'][0][field]
        with pytest.raises(StoreConflict, match='object_purged'):
            save(store, attempted, 4, 'reuse-'+field)
    with pytest.raises(KnowledgeError):
        save(store, combined, 4, 'old-full-payload')
    with pytest.raises(StoreConflict, match='source_withdrawn'):
        seed(store, inferred)


def test_cleanup_removes_complete_revision_chain(store):
    f = fixture('corrected_preference')
    seed(store, f)
    save(store, f)
    scope = f['bindings']['scope']
    store.withdraw_source(scope, f['payload']['objects'][0]['sources'][0])
    assert purge_withdrawn(store, scope)['removed_objects'] == 2
    assert store.export_subject(scope)['payload'] is None
    # Even a still-authorized correction cannot reuse the purged assertion.
    fresh = fixture()
    fresh['bindings']['sources'][0]['source_id'] = 'fresh-correction'
    fresh['payload']['objects'][0]['sources'][0]['source_id'] = 'fresh-correction'
    seed(store, fresh)
    fresh['payload']['objects'][0]['assertion_id'] = f['payload']['objects'][0]['assertion_id']
    from cart_recovery.domain.store import StoreConflict
    with pytest.raises(StoreConflict, match='object_purged'):
        save(store, fresh, 3, 'restart-history')


def test_cleanup_failure_rolls_back_every_row(store, database):
    f = fixture('inferred_preference')
    seed(store, f)
    save(store, f)
    scope = f['bindings']['scope']
    store.withdraw_source(scope, f['payload']['objects'][0]['sources'][0])
    before = store.export_subject(scope)
    checkpoint = store.retained_checkpoint()
    with psycopg.connect(database) as db:
        db.execute("CREATE FUNCTION fail_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture'; END $$")
        db.execute('CREATE TRIGGER fail_cleanup BEFORE INSERT ON knowledge_retained.events FOR EACH ROW EXECUTE FUNCTION fail_cleanup()')
    with pytest.raises(psycopg.Error):
        purge_withdrawn(store, scope)
    assert store.export_subject(scope) == before
    assert store.retained_checkpoint() == checkpoint


def test_parallel_cleanup_and_erasure_are_serialized(store):
    from concurrent.futures import ThreadPoolExecutor
    from cart_recovery.domain.store import StoreConflict
    f = fixture()
    seed(store, f)
    save(store, f)
    scope = f['bindings']['scope']
    store.withdraw_source(scope, f['payload']['objects'][0]['sources'][0])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: purge_withdrawn(store, scope), range(4)))
    assert sum(r['removed_sources'] for r in results) == 1
    assert {r['version'] for r in results} == {3}
    with ThreadPoolExecutor(max_workers=2) as pool:
        erasure = pool.submit(store.erase_subject, scope)
        cleanup = pool.submit(purge_withdrawn, store, scope)
        erasure.result()
        try:
            cleanup.result()
        except StoreConflict as exc:
            assert str(exc) == 'subject_fenced'
    with pytest.raises(StoreConflict):
        store.current(scope)


def test_cleanup_tenant_quarantine_and_identity_binding(store):
    from cart_recovery.domain.store import StoreConflict
    a, b = fixture(), fixture(merchant='merchant-b')
    for f in (a, b):
        seed(store, f)
        save(store, f)
    scope = a['bindings']['scope']
    store.withdraw_source(scope, a['payload']['objects'][0]['sources'][0])
    for wrong in ({**scope, 'store_id': 'wrong'}, {**scope, 'privacy_generation': 2}):
        with pytest.raises(StoreConflict):
            purge_withdrawn(store, wrong)
    purge_withdrawn(store, scope)
    assert len(store.current(b['bindings']['scope'])['objects']) == 1
    store.quarantine()
    with pytest.raises(StoreConflict, match='quarantined'):
        purge_withdrawn(store, scope)


def test_checkpoint_restores_purge_tombstones_and_refuses_old_backup(store, database, cluster, tmp_path):
    import subprocess
    import uuid
    from psycopg import sql
    from conftest import PG, ROOT
    from test_lifecycle import KEY
    from cart_recovery.domain.store import KnowledgeStore, StoreConflict, digest
    f = fixture()
    seed(store, f)
    save(store, f)
    scope = f['bindings']['scope']
    backup = tmp_path/'before-purge.sql'
    subprocess.run([str(PG/'pg_dump'), '--dbname', database, '--data-only', '--schema=knowledge_data', '--file', str(backup)], check=True, capture_output=True)
    store.withdraw_source(scope, f['payload']['objects'][0]['sources'][0])
    purge_withdrawn(store, scope)
    checkpoint = store.retained_checkpoint()
    clean_backup = tmp_path/'after-purge.sql'
    subprocess.run([str(PG/'pg_dump'), '--dbname', database, '--data-only', '--schema=knowledge_data', '--file', str(clean_backup)], check=True, capture_output=True)
    assert f['payload']['objects'][0]['object_id'] not in json.dumps(checkpoint)
    params, _ = cluster
    name = 'restore_'+uuid.uuid4().hex
    with psycopg.connect(**params, autocommit=True) as db:
        db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    dsn = psycopg.conninfo.make_conninfo(**{**params, 'dbname': name})
    try:
        with psycopg.connect(dsn) as db:
            db.execute((ROOT/'cart_recovery/domain/migrations/001_knowledge.sql').read_text())
        subprocess.run([str(PG/'psql'), '--dbname', dsn, '--set', 'ON_ERROR_STOP=1', '--file', str(backup)], check=True, capture_output=True)
        restored = KnowledgeStore(dsn, KEY)
        restored.restore_fences(checkpoint, expected_digest=digest(checkpoint))
        with pytest.raises(StoreConflict, match='restore_gap'):
            restored.release_quarantine()
        # Even manually relabeling stale data with the current version cannot
        # bypass retained object/assertion tombstones.
        with psycopg.connect(dsn) as db:
            db.execute('UPDATE knowledge_data.subjects SET data_version=3')
        with pytest.raises(StoreConflict, match='object_purged'):
            restored.release_quarantine()
        with pytest.raises(StoreConflict, match='quarantined'):
            restored.current(scope)
        assert restored.retained_checkpoint() == checkpoint
        with psycopg.connect(dsn) as db:
            db.execute('TRUNCATE knowledge_data.subjects CASCADE')
        subprocess.run([str(PG/'psql'), '--dbname', dsn, '--set', 'ON_ERROR_STOP=1', '--file', str(clean_backup)], check=True, capture_output=True)
        restored.release_quarantine()
        assert restored.current(scope)['objects'] == []
        assert restored.export_subject(scope)['sources'] == []
        fresh = fixture()
        fresh['bindings']['sources'][0]['source_id'] = 'new-authorized-source'
        fresh['payload']['objects'][0]['sources'][0]['source_id'] = 'new-authorized-source'
        seed(restored, fresh)
        with pytest.raises(StoreConflict, match='object_purged'):
            save(restored, fresh, 3, 'restore-reuse')
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


@pytest.fixture
def export_store(store, database):
    from conftest import ROOT
    with psycopg.connect(database) as db:
        db.execute((ROOT/'cart_recovery/domain/migrations/002_delivery_receipts.sql').read_text())
    return store


def test_export_pages_cover_retained_metadata_after_erasure(export_store):
    store = export_store
    f = fixture('inferred_preference')
    seed(store, f)
    save(store, f)
    scope = f['bindings']['scope']
    store.withdraw_source(scope, f['payload']['objects'][0]['sources'][0])
    purge_withdrawn(store, scope)
    store.erase_subject(scope)
    pages, snapshot = {}, None
    for family in ('withdrawals', 'events', 'delivery_receipts'):
        rows, cursor = [], None
        while True:
            page = export_retained_page(store, scope, family, limit=1, cursor=cursor, snapshot=snapshot)
            snapshot = page['snapshot']
            assert page['subject']['erased'] is True
            assert page['whole_application_complete'] is False
            rows += page['rows']
            cursor = page['next_cursor']
            if cursor is None:
                break
        assert len(rows) == page['counts'][family]
        assert len({json.dumps(r) for r in rows}) == len(rows)
        pages[family] = rows
    assert len(pages['withdrawals']) == 5  # one source, two objects, two assertions
    assert [r[2] for r in pages['events']] == ['write', 'source_withdrawal', 'write', 'subject_erasure']
    store.erase_merchant(scope['merchant_id'])
    merchant_page = export_retained_page(store, scope, 'events')
    assert merchant_page['merchant_erased'] is True
    assert merchant_page['rows'][-1][2] == 'merchant_erasure'


def test_export_cursor_scope_tampering_and_changes_fail_closed(export_store):
    from cart_recovery.domain.store import StoreConflict
    store = export_store
    a, b = fixture(), fixture(merchant='merchant-b')
    for f in (a, b):
        seed(store, f)
        save(store, f)
        store.withdraw_source(f['bindings']['scope'], f['payload']['objects'][0]['sources'][0])
    scope = a['bindings']['scope']
    page = export_retained_page(store, scope, 'events', limit=1)
    cursor = page['next_cursor']
    assert cursor is not None
    for wrong_scope, wrong_cursor, family in [
        (b['bindings']['scope'], cursor, 'events'),
        (scope, {**cursor, 'after': 9999}, 'events'),
        (scope, cursor, 'withdrawals'),
        (scope, {**cursor, 'mac': 'bad'}, 'events'),
        (scope, {**cursor, 'mac': 'é'*64}, 'events'),
        (scope, {**cursor, 'after': 2**80}, 'events'),
        (scope, {**cursor, 'snapshot': []}, 'events'),
    ]:
        with pytest.raises(StoreConflict, match='export_cursor'):
            export_retained_page(store, wrong_scope, family, cursor=wrong_cursor)
    purge_withdrawn(store, scope)
    with pytest.raises(StoreConflict, match='export_changed'):
        export_retained_page(store, scope, 'events', cursor=cursor)
    with pytest.raises(StoreConflict, match='export_changed'):
        export_retained_page(store, scope, 'withdrawals', snapshot=page['snapshot'])
    for limit in (0, 101, True, '1'):
        with pytest.raises(StoreConflict):
            export_retained_page(store, scope, 'events', limit=limit)
    with pytest.raises(StoreConflict):
        export_retained_page(store, {**scope, 'privacy_generation': 2}, 'events')
    store.quarantine()
    with pytest.raises(StoreConflict):
        export_retained_page(store, scope, 'events')


def test_export_refuses_missing_receipt_inventory(store):
    f = fixture()
    seed(store, f)
    with pytest.raises(psycopg.errors.UndefinedTable):
        export_retained_page(store, f['bindings']['scope'], 'withdrawals')


def test_delivery_receipts_are_exported_but_do_not_claim_physical_cleanup(export_store):
    from cart_recovery.domain.delivery import create_delivery_app
    from test_delivery import GRANT, raw_command, source_command, lookup, post
    from test_service import data
    store = export_store
    app = create_delivery_app(enabled=True, grants=[GRANT], store=store)
    scope = data()['bindings']['scope']
    assert post(app, raw_command(scope)).status_code == 200
    source = source_command(scope)
    assert post(app, source).status_code == 200
    receipt = json.loads(source)['arguments']['receipt']
    withdrawal = raw_command(scope, 'withdraw_source', {'source_ref': {'source_id': receipt['source_id'], 'revision': receipt['revision']}})
    assert post(app, withdrawal).status_code == 200
    assert post(app, lookup(withdrawal), 'receipt').json['result']['status'] == 'applied'
    assert len(store.export_subject(scope)['sources']) == 1
    assert purge_withdrawn(store, scope)['removed_sources'] == 1
    # Historical receipt replay cannot restore bytes or falsely report a new purge.
    assert post(app, source).json['result']['status'] == 'applied'
    assert post(app, withdrawal).json['result']['status'] == 'applied'
    assert store.export_subject(scope)['sources'] == []
    page = export_retained_page(store, scope, 'delivery_receipts', limit=1)
    assert page['counts']['delivery_receipts'] == 3
    rows = page['rows']
    while page['next_cursor']:
        page = export_retained_page(store, scope, 'delivery_receipts', limit=1, cursor=page['next_cursor'])
        rows += page['rows']
    assert sorted(r[3] for r in rows) == ['scope_sync', 'source', 'withdraw_source']
    assert post(app, raw_command(scope, 'erase_subject')).status_code == 200
    assert export_retained_page(store, scope, 'delivery_receipts')['counts']['delivery_receipts'] == 4


def test_authenticated_write_cannot_reintroduce_purged_identifiers(store):
    from copy import deepcopy
    from cart_recovery.domain.service import create_knowledge_service_app, PREFIX
    from test_service import data, GRANT, post, seed as api_seed, save as api_save
    app = create_knowledge_service_app(enabled=True, grants=[GRANT], store=store)
    value = data()
    api_seed(app, value)
    assert api_save(app, value).status_code == 200
    scope = value['bindings']['scope']
    ref = value['payload']['objects'][0]['sources'][0]
    assert post(app, value, 'withdraw_source', {'source_ref': ref}).status_code == 200
    purge_withdrawn(store, scope)
    new = deepcopy(value)
    new['bindings']['sources'][0]['source_id'] = 'independent-statement'
    new['payload']['objects'][0]['sources'][0]['source_id'] = 'independent-statement'
    api_seed(app, new)
    response = api_save(app, new, 3, 'reused-id')
    assert response.status_code == 409
    assert response.json['error'] == 'knowledge_refused'
    new['payload']['objects'][0].update(object_id='fresh-object', assertion_id='fresh-assertion')
    assert api_save(app, new, 3, 'fresh-id').status_code == 200
    for name in ('purge_withdrawn', 'export_retained_page'):
        assert app.test_client().post(PREFIX+name).status_code == 404
