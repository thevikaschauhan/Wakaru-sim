"""Durable cleanup receipts against disposable PostgreSQL, never shared state."""
import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from test_lifecycle import fixture, seed, save, store
from cart_recovery.domain.cleanup import CleanupStore, create_cleanup_app
from cart_recovery.domain.delivery import DeliveryStore
from cart_recovery.domain.store import StoreConflict

ROOT = Path(__file__).resolve().parents[2]


def stage(store, database):
    with psycopg.connect(database) as db:
        for name in ('002_delivery_receipts.sql', '003_cleanup_receipts.sql'):
            db.execute((ROOT/'cart_recovery/domain/migrations'/name).read_text())
    f = fixture('inferred_preference', merchant='11111111-1111-4111-8111-111111111111')
    f = json.loads(json.dumps(f).replace('"store-a"', '"1"'))
    seed(store, f)
    save(store, f)
    scope = f['bindings']['scope']
    withdrawal = dict(version='knowledge_delivery_command_v1', command_id=str(uuid.uuid4()),
                      operation='withdraw_source', scope=scope,
                      arguments={'source_ref': f['payload']['objects'][0]['sources'][0]}, expires_at=None)
    # Source refs in fixtures also carry provenance properties. Delivery accepts
    # only the immutable source/revision selector for withdrawal.
    withdrawal['arguments']['source_ref'] = {k: withdrawal['arguments']['source_ref'][k] for k in ('source_id','revision')}
    wire = json.dumps(withdrawal).encode()
    DeliveryStore(store).handle(wire)
    with store.transaction() as db:
        version = store.bound(db, scope)[3]
    command = dict(version='knowledge_cleanup_request_v1', operation='purge_withdrawn',
                   command_id=str(uuid.uuid4()), scope=scope, withdrawal_id=withdrawal['command_id'],
                   withdrawal_sha256=hashlib.sha256(wire).hexdigest(), expected_version=version)
    return f, command


def test_cleanup_receipt_atomic_replay_and_after_erasure(store, database):
    f, command = stage(store, database)
    raw = json.dumps(command).encode()
    first = CleanupStore(store).handle(raw)
    assert first['status'] == 'applied'
    assert first['result']['removed_sources'] == 1
    assert first['result']['removed_objects'] == 2
    assert store.export_subject(command['scope'])['payload'] is None
    assert CleanupStore(store).handle(raw) == first
    store.erase_subject(command['scope'])
    assert CleanupStore(store).handle(raw, lookup=True) == first
    assert CleanupStore(store).handle(raw) == first  # receipt replay, no data mutation
    with pytest.raises(StoreConflict, match='cleanup_rebound'):
        CleanupStore(store).handle(json.dumps({**command, 'expected_version': 0}).encode())


def test_cleanup_missing_receipt_and_wrong_withdrawal(store, database):
    _, command = stage(store, database)
    raw = json.dumps(command).encode()
    assert CleanupStore(store).handle(raw, lookup=True)['status'] == 'unknown'
    with pytest.raises(StoreConflict, match='withdrawal_unproven'):
        CleanupStore(store).handle(json.dumps({**command, 'withdrawal_sha256':'0'*64}).encode())
    wrong = {**command, 'scope': {**command['scope'], 'merchant_id':'merchant-b'}}
    with pytest.raises(StoreConflict):
        CleanupStore(store).handle(json.dumps(wrong).encode())
    assert CleanupStore(store).handle(raw, lookup=True)['status'] == 'unknown'


def test_cleanup_version_refusal_is_durable(store, database):
    _, command = stage(store, database)
    stale = json.dumps({**command, 'expected_version': 0}).encode()
    refused = CleanupStore(store).handle(stale)
    assert refused['status'] == 'refused'
    assert refused['result']['removed_sources'] == refused['result']['removed_objects'] == 0
    assert CleanupStore(store).handle(stale, lookup=True) == refused
    fresh = {**command, 'command_id': str(uuid.uuid4())}
    assert CleanupStore(store).handle(json.dumps(fresh).encode())['status'] == 'applied'
    assert CleanupStore(store).handle(stale) == refused


def test_cleanup_receipt_failure_rolls_back_purge(store, database):
    _, command = stage(store, database)
    before = store.export_subject(command['scope'])
    checkpoint = store.retained_checkpoint()
    with psycopg.connect(database) as db:
        db.execute("CREATE FUNCTION fail_cleanup_receipt() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture'; END $$")
        db.execute('CREATE TRIGGER fail_cleanup_receipt BEFORE INSERT ON knowledge_retained.cleanup_receipts FOR EACH ROW EXECUTE FUNCTION fail_cleanup_receipt()')
    with pytest.raises(psycopg.Error):
        CleanupStore(store).handle(json.dumps(command).encode())
    assert store.export_subject(command['scope']) == before
    assert store.retained_checkpoint() == checkpoint
    assert CleanupStore(store).handle(json.dumps(command).encode(), lookup=True)['status'] == 'unknown'


def test_cleanup_concurrent_versions_and_immutable_receipt(store, database):
    _, command = stage(store, database)
    commands = [json.dumps({**command, 'command_id':str(uuid.uuid4())}).encode() for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(CleanupStore(store).handle, commands))
    assert sorted(r['status'] for r in results) == ['applied','refused']
    assert sum(r['result']['removed_sources'] for r in results) == 1
    for statement in ('DELETE FROM knowledge_retained.cleanup_receipts',
                      'TRUNCATE knowledge_retained.cleanup_receipts',
                      "UPDATE knowledge_retained.cleanup_receipts SET status='refused'"):
        with pytest.raises(psycopg.Error):
            with psycopg.connect(database) as db:
                db.execute(statement)


def test_cleanup_fenced_and_quarantined_attempts_stay_unknown(store, database):
    _, command = stage(store, database)
    raw = json.dumps(command).encode()
    store.erase_subject(command['scope'])
    with pytest.raises(StoreConflict):
        CleanupStore(store).handle(raw)
    assert CleanupStore(store).handle(raw, lookup=True)['status'] == 'unknown'
    with psycopg.connect(database) as db:
        db.execute('UPDATE knowledge_retained.control SET quarantined=true')
    with pytest.raises(StoreConflict):
        CleanupStore(store).handle(raw, lookup=True)


def test_cleanup_default_off_and_unauthenticated(store, database):
    _, command = stage(store, database)
    path='/internal/integrations/knowledge-cleanup/purge'
    assert create_cleanup_app().test_client().post(path,json=command).status_code==404
    with pytest.raises(ValueError):
        create_cleanup_app(enabled=True,store=store)
    grant=dict(key_id='fixture',secret='fixture-only-cleanup-key-32-bytes-long',merchant_id=command['scope']['merchant_id'],store_id=command['scope']['store_id'],audience='wakaru-knowledge-cleanup',operations=['POST '+path])
    assert create_cleanup_app(enabled=True,store=store,grants=[grant]).test_client().post(path,json=command).status_code==401


def test_cleanup_authentication_and_operation_boundaries(store, database):
    import hmac
    import time
    from cart_recovery.domain.cleanup import AUDIENCE, PREFIX
    _, command = stage(store, database)
    grant = dict(key_id='fixture',secret='fixture-only-cleanup-key-32-bytes-long',
                 merchant_id=command['scope']['merchant_id'],store_id=command['scope']['store_id'],
                 audience=AUDIENCE,operations=['POST '+PREFIX+op for op in ('purge','receipt')])
    app = create_cleanup_app(enabled=True,store=store,grants=[grant])

    def post(value, op='purge', signer=grant, target=app):
        raw = json.dumps(value).encode()
        stamp = str(int(time.time()))
        path = PREFIX+op
        frame = '\n'.join(('integration_service_request_v1',signer['key_id'],signer['audience'],signer['merchant_id'],signer['store_id'],'POST',path,stamp,hashlib.sha256(raw).hexdigest()))
        header = 'Integration v1.'+signer['key_id']+'.'+stamp+'.'+hmac.new(signer['secret'].encode(),frame.encode(),hashlib.sha256).hexdigest()
        return target.test_client().post(path,data=raw,headers={'Authorization':header})

    assert post(command, signer={**grant,'audience':'wakaru-knowledge-privacy'}).status_code==401
    assert post({**command,'scope':{**command['scope'],'merchant_id':'other'}}).status_code==403
    assert post({**command,'unexpected':True}).status_code==400
    assert post(command, op='erase_subject').status_code==404
    receipt_only=create_cleanup_app(enabled=True,store=store,grants=[{**grant,'operations':['POST '+PREFIX+'receipt']}])
    assert post(command,op='receipt',target=receipt_only).status_code==403
    assert post(command,op='receipt').json['status']=='unknown'
    applied=post(command)
    assert applied.status_code==200 and applied.json['status']=='applied'
    assert applied.headers['Cache-Control']=='no-store'
    assert post(command,op='receipt').json==applied.json
