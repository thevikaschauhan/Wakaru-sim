"""Real PostgreSQL dump/restore, receipt history and bounded export coverage."""
from copy import deepcopy
import json
import subprocess
import uuid

import psycopg
from psycopg import sql
import pytest

from conftest import PG, ROOT
from test_lifecycle import store, KEY
from test_cleanup_receipts import stage
from cart_recovery.domain.cleanup import CleanupStore
from cart_recovery.domain.checkpoint import retained_checkpoint, restore_checkpoint, release_checkpoint
from cart_recovery.domain.privacy import export_retained_page
from cart_recovery.domain.store import KnowledgeStore, StoreConflict, digest


@pytest.fixture
def restored(cluster):
    params, _ = cluster
    name = 'restore_'+uuid.uuid4().hex
    with psycopg.connect(**params,autocommit=True) as db:
        db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    dsn = psycopg.conninfo.make_conninfo(**{**params,'dbname':name})
    try:
        with psycopg.connect(dsn) as db:
            for migration in ('001_knowledge.sql','002_delivery_receipts.sql','003_cleanup_receipts.sql'):
                db.execute((ROOT/'cart_recovery/domain/migrations'/migration).read_text())
        yield KnowledgeStore(dsn,KEY)
    finally:
        with psycopg.connect(**params,autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def dump_data(store,path):
    subprocess.run([str(PG/'pg_dump'),'--dbname',store.dsn,'--data-only','--schema=knowledge_data','--file',str(path)],check=True,capture_output=True)


def load_data(store,path):
    subprocess.run([str(PG/'psql'),'--dbname',store.dsn,'-X','--set','ON_ERROR_STOP=1','--file',str(path)],check=True,capture_output=True)


def test_cleanup_recovery_preserves_receipts_and_rejects_old_data(store,database,restored,tmp_path):
    _,command=stage(store,database)
    stale=tmp_path/'stale.sql';clean=tmp_path/'clean.sql'
    dump_data(store,stale)
    raw=json.dumps(command).encode()
    refused_raw=json.dumps({**command,'command_id':str(uuid.uuid4()),'expected_version':0}).encode()
    refused=CleanupStore(store).handle(refused_raw)
    applied=CleanupStore(store).handle(raw)
    checkpoint=retained_checkpoint(store);trusted=digest(checkpoint)
    dump_data(store,clean)
    load_data(restored,stale)
    restore_checkpoint(restored,checkpoint,expected_digest=trusted)
    with pytest.raises(StoreConflict,match='restore_gap'):
        release_checkpoint(restored,expected_digest=trusted)
    with pytest.raises(StoreConflict,match='quarantined'):
        CleanupStore(restored).handle(raw,lookup=True)
    with psycopg.connect(restored.dsn) as db:
        db.execute('TRUNCATE knowledge_data.subjects CASCADE')
    load_data(restored,clean)
    # Reapplying an identical checkpoint is safe, including immutable receipts.
    restore_checkpoint(restored,checkpoint,expected_digest=trusted)
    release_checkpoint(restored,expected_digest=trusted)
    assert CleanupStore(restored).handle(raw,lookup=True)==applied
    assert CleanupStore(restored).handle(raw)==applied
    assert CleanupStore(restored).handle(refused_raw)==refused
    assert retained_checkpoint(restored)==checkpoint
    assert restored.export_subject(command['scope'])['sources']==[]
    unknown=json.dumps({**command,'command_id':str(uuid.uuid4())}).encode()
    assert CleanupStore(restored).handle(unknown,lookup=True)['status']=='unknown'
    restored.erase_subject(command['scope'])
    # Restored event IDs are followed by new IDs, never collided/overwritten.
    assert retained_checkpoint(restored)['events'][-1][0]>checkpoint['events'][-1][0]
    assert CleanupStore(restored).handle(raw,lookup=True)==applied


def test_cleanup_recovery_erased_subject_cannot_resurrect_from_data_backup(store,database,restored,tmp_path):
    _,command=stage(store,database);raw=json.dumps(command).encode()
    backup=tmp_path/'active.sql';dump_data(store,backup)
    applied=CleanupStore(store).handle(raw)
    store.erase_subject(command['scope'])
    checkpoint=retained_checkpoint(store)
    load_data(restored,backup)
    restore_checkpoint(restored,checkpoint,expected_digest=digest(checkpoint))
    release_checkpoint(restored,expected_digest=digest(checkpoint))
    assert CleanupStore(restored).handle(raw,lookup=True)==applied
    with pytest.raises(StoreConflict):restored.current(command['scope'])
    with psycopg.connect(restored.dsn) as db:
        assert db.execute('SELECT count(*) FROM knowledge_data.subjects').fetchone()[0]==0


def test_cleanup_recovery_validation_and_conflict_are_atomic(store,database,restored):
    _,command=stage(store,database);CleanupStore(store).handle(json.dumps(command).encode())
    checkpoint=retained_checkpoint(store)
    for mutation in ('digest','missing','duplicate','orphan','wrong_key','result'):
        bad=deepcopy(checkpoint)
        if mutation=='missing':del bad['cleanup_receipts']
        elif mutation=='duplicate':bad['cleanup_receipts'].append(bad['cleanup_receipts'][0])
        elif mutation=='orphan':bad['cleanup_receipts'][0][0]='f'*64
        elif mutation=='wrong_key':bad['fences']['key_fingerprint']='f'*64
        elif mutation=='result':bad['cleanup_receipts'][0][-1]['whole_application_complete']=True
        with pytest.raises(StoreConflict):
            restore_checkpoint(restored,bad,expected_digest='0'*64 if mutation=='digest' else digest(bad))
        with psycopg.connect(restored.dsn) as db:
            assert db.execute('SELECT count(*) FROM knowledge_retained.subjects').fetchone()[0]==0
    restore_checkpoint(restored,checkpoint,expected_digest=digest(checkpoint))
    for family in ('cleanup_receipts','delivery_receipts','events'):
        bad=deepcopy(checkpoint);bad[family]=[]
        with pytest.raises(StoreConflict,match='conflict_or_regression'):
            restore_checkpoint(restored,bad,expected_digest=digest(bad))
        assert retained_checkpoint(restored)==checkpoint
    bad=deepcopy(checkpoint);bad['cleanup_receipts'][0][2]='f'*64
    with pytest.raises(StoreConflict,match='conflict_or_regression'):
        restore_checkpoint(restored,bad,expected_digest=digest(bad))
    assert retained_checkpoint(restored)==checkpoint
    with pytest.raises(StoreConflict,match='checkpoint_binding'):
        release_checkpoint(restored,expected_digest='f'*64)


def test_cleanup_recovery_missing_migration_fails_closed(store,database):
    with pytest.raises(psycopg.errors.UndefinedTable):retained_checkpoint(store)


def test_cleanup_export_paginates_refusals_and_detects_receipt_only_changes(store,database):
    _,command=stage(store,database);scope=command['scope']
    for _ in range(3):
        CleanupStore(store).handle(json.dumps({**command,'command_id':str(uuid.uuid4()),'expected_version':0}).encode())
    first=export_retained_page(store,scope,'cleanup_receipts',limit=1)
    rows=first['rows'];page=first
    while page['next_cursor']:
        page=export_retained_page(store,scope,'cleanup_receipts',limit=1,cursor=page['next_cursor'],snapshot=first['snapshot']);rows+=page['rows']
    assert len(rows)==3 and len({r[0] for r in rows})==3
    assert all(r[3]=='refused' and r[4]['removed_sources']==0 for r in rows)
    for wrong in ({**scope,'store_id':'2'},{**scope,'subject_id':'another-shopper'}):
        with pytest.raises(StoreConflict):export_retained_page(store,wrong,'cleanup_receipts',cursor=first['next_cursor'])
    with pytest.raises(StoreConflict):export_retained_page(store,scope,'delivery_receipts',cursor=first['next_cursor'])
    # Refusal adds no event/version, but it must invalidate every export family.
    CleanupStore(store).handle(json.dumps({**command,'command_id':str(uuid.uuid4()),'expected_version':0}).encode())
    with pytest.raises(StoreConflict,match='export_changed'):
        export_retained_page(store,scope,'events',snapshot=first['snapshot'])
    CleanupStore(store).handle(json.dumps(command).encode());store.erase_subject(scope)
    final=export_retained_page(store,scope,'cleanup_receipts')
    assert final['subject']['erased'] and len(final['rows'])==5
    assert final['whole_application_complete'] is False
    assert command['withdrawal_id'] not in json.dumps(final)


def test_cleanup_recovery_legacy_release_cannot_bypass_receipts(store,database,restored,tmp_path):
    _,command=stage(store,database)
    CleanupStore(store).handle(json.dumps(command).encode())
    current=retained_checkpoint(store)
    backup=tmp_path/'clean.sql';dump_data(store,backup);load_data(restored,backup)
    # Old fence-only overlay must not qualify the receipt-aware database.
    restored.restore_fences(current['fences'],expected_digest=digest(current['fences']))
    with pytest.raises(StoreConflict,match='ledger_checkpoint_required'):
        restored.release_quarantine()
    with pytest.raises(StoreConflict,match='ledger_checkpoint_required'):
        restored.release_quarantine(expected_checkpoint_digest=digest(current))
    restore_checkpoint(restored,current,expected_digest=digest(current))
    release_checkpoint(restored,expected_digest=digest(current))


def test_cleanup_recovery_requires_cleanup_table_and_receipt_empty_bootstrap(store,database,restored):
    with psycopg.connect(database) as db:
        db.execute((ROOT/'cart_recovery/domain/migrations/002_delivery_receipts.sql').read_text())
    with pytest.raises(psycopg.errors.UndefinedTable):retained_checkpoint(store)
    with psycopg.connect(restored.dsn) as db:
        db.execute("INSERT INTO knowledge_retained.delivery_receipts VALUES(%s,%s,%s,%s,'withdraw_source')",('a'*64,'b'*64,'c'*64,'d'*64))
    with pytest.raises(StoreConflict,match='bootstrap_not_empty'):
        restored.bootstrap_empty()
