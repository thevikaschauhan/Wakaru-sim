"""Command atomicity, scope fencing and historical receipts on real PostgreSQL."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import time
import uuid

import psycopg
import pytest

from cart_recovery.domain.delivery import AUDIENCE, PREFIX, OPERATIONS, DeliveryStore, create_delivery_app
from cart_recovery.domain.store import KnowledgeStore
from test_service import data, KEY, GRANT as OLD_GRANT

GRANT = dict(OLD_GRANT, audience=AUDIENCE, operations=['POST '+PREFIX+op for op in (*OPERATIONS,'receipt')])


def raw_command(scope, operation='scope_sync', arguments=None, **kw):
    value=dict(version='knowledge_delivery_command_v1',command_id=str(uuid.uuid4()),operation=operation,scope=scope,arguments=arguments or {},expires_at=None)
    value.update(kw)
    return json.dumps(value,separators=(',',':')).encode()


def lookup(raw):
    c=json.loads(raw)
    return json.dumps(dict(version='knowledge_delivery_lookup_v1',command_id=c['command_id'],operation=c['operation'],scope=c['scope'],command_sha256=hashlib.sha256(raw).hexdigest())).encode()


def auth(raw, op, grant=GRANT):
    stamp=str(int(time.time()))
    frame='\n'.join(('integration_service_request_v1',grant['key_id'],grant['audience'],grant['merchant_id'],grant['store_id'],'POST',PREFIX+op,stamp,hashlib.sha256(raw).hexdigest()))
    return 'Integration v1.'+grant['key_id']+'.'+stamp+'.'+hmac.new(grant['secret'].encode(),frame.encode(),hashlib.sha256).hexdigest()


def post(app,raw,op=None,grant=GRANT):
    op=op or json.loads(raw)['operation']
    return app.test_client().post(PREFIX+op,data=raw,headers={'Authorization':auth(raw,op,grant)})


@pytest.fixture
def delivery(database):
    with psycopg.connect(database) as db:
        db.execute((Path(__file__).resolve().parents[2]/'cart_recovery/domain/migrations/002_delivery_receipts.sql').read_text())
    store=KnowledgeStore(database,KEY);store.bootstrap_empty()
    return create_delivery_app(enabled=True,grants=[GRANT],store=store),store


def source_command(scope):
    receipt=data()['bindings']['sources'][0]
    receipt['scope']=scope
    content=json.dumps(receipt['claims']).encode()
    receipt['content_sha256']=hashlib.sha256(content).hexdigest()
    return raw_command(scope,'source',dict(receipt=receipt,content_base64=base64.b64encode(content).decode()),expires_at=(datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat().replace('+00:00','Z'))


def test_delivery_receipt_survives_restart_advance_and_erasure(delivery,database):
    app,store=delivery;scope=data()['bindings']['scope']
    provision=raw_command(scope)
    first=post(app,provision);assert first.status_code==200
    source=source_command(scope);assert post(app,source).status_code==200
    assert len(store.export_subject(scope)['sources'])==1
    new=dict(scope,privacy_generation=scope['privacy_generation']+1)
    advance=raw_command(new);assert post(app,advance).status_code==200
    assert store.export_subject(new)['sources']==[]
    assert post(app,raw_command(scope)).status_code==409
    # A previously applied source can be acknowledged without executing again.
    assert post(app,source).json['result']['status']=='applied'
    assert store.export_subject(new)['sources']==[]
    erased=dict(new,privacy_generation=new['privacy_generation']+1)
    erase=raw_command(erased,'erase_subject');assert post(app,erase).status_code==200
    restarted=create_delivery_app(enabled=True,grants=[GRANT],store=KnowledgeStore(database,KEY))
    for raw in (provision,source,advance,erase):
        result=post(restarted,lookup(raw),'receipt')
        assert result.status_code==200 and result.json['result']['status']=='applied'
    assert post(restarted,raw_command(dict(erased,privacy_generation=erased['privacy_generation']+1))).status_code==409
    assert post(restarted,raw_command(dict(erased,privacy_generation=erased['privacy_generation']+1),'erase_subject')).status_code==200
    with psycopg.connect(database) as db:
        assert db.execute('SELECT count(*) FROM knowledge_data.subjects').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM knowledge_retained.delivery_receipts').fetchone()[0]==5


def test_delivery_atomic_receipt_failure_rolls_back_mutation(delivery,database):
    app,store=delivery;scope=data()['bindings']['scope'];raw=raw_command(scope)
    with psycopg.connect(database) as db:
        db.execute("CREATE FUNCTION fail_delivery_receipt() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture'; END $$")
        db.execute('CREATE TRIGGER fail_receipt BEFORE INSERT ON knowledge_retained.delivery_receipts FOR EACH ROW EXECUTE FUNCTION fail_delivery_receipt()')
    assert post(app,raw).status_code==503
    assert store.retained_checkpoint()['subjects']==[]
    assert post(app,lookup(raw),'receipt').json['result']['status']=='unknown'
    with psycopg.connect(database) as db:db.execute('DROP TRIGGER fail_receipt ON knowledge_retained.delivery_receipts')
    assert post(app,raw).status_code==200


def test_delivery_duplicate_and_rebinding(delivery):
    app,store=delivery;scope=data()['bindings']['scope'];raw=raw_command(scope)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _:post(app,raw).status_code,range(6)))
    assert results==[200]*6
    changed=json.loads(raw);changed['operation']='erase_subject'
    assert post(app,json.dumps(changed).encode()).status_code==409
    assert store.current(scope)['version']==0
    altered=json.loads(lookup(raw));altered['command_sha256']='0'*64
    assert post(app,json.dumps(altered).encode(),'receipt').status_code==409


@pytest.mark.parametrize('attack',['wrong_store','wrong_audience','wrong_operation','duplicate_fields','extra_field','unknown_route','expired_source','missing_deadline','identity_without_privacy'])
def test_delivery_refuses_invalid_or_stale_commands(delivery,attack):
    app,store=delivery;scope=data()['bindings']['scope'];raw=raw_command(scope)
    assert post(app,raw).status_code==200
    grant=GRANT;op=None;expected=400
    if attack=='wrong_store':raw=raw_command(dict(scope,store_id='2'));expected=403
    elif attack=='wrong_audience':grant=dict(GRANT,audience='wakaru-knowledge');expected=401
    elif attack=='wrong_operation':op='erase_subject';expected=403
    elif attack=='duplicate_fields':raw=raw[:-1]+b',"operation":"scope_sync"}';expected=409
    elif attack=='extra_field':raw=raw[:-1]+b',"admin":true}'
    elif attack=='unknown_route':op='release_quarantine';expected=404
    elif attack in ('expired_source','missing_deadline'):
        c=json.loads(source_command(scope));c['expires_at']='2020-01-01T00:00:00Z' if attack=='expired_source' else None
        raw=json.dumps(c).encode();expected=409
    elif attack=='identity_without_privacy':raw=raw_command(dict(scope,identity_revision=scope['identity_revision']+1));expected=409
    assert post(app,raw,op,grant).status_code==expected
    assert store.current(scope)['version']==0


def test_delivery_quarantine_and_default_off(delivery):
    app,store=delivery;raw=raw_command(data()['bindings']['scope'])
    assert post(create_delivery_app(store=store),raw).status_code==404
    assert post(app,raw).status_code==200
    store.quarantine()
    assert post(app,lookup(raw),'receipt').status_code==409
    assert post(app,raw).status_code==409


def test_delivery_receipt_requires_original_operation_grant(delivery):
    _,store=delivery
    app=create_delivery_app(enabled=True,grants=[dict(GRANT,operations=['POST '+PREFIX+'receipt'])],store=store)
    assert post(app,lookup(raw_command(data()['bindings']['scope'])),'receipt').status_code==403


def test_delivery_erase_before_first_arrival(delivery):
    app,store=delivery;scope=data()['bindings']['scope']
    assert post(app,raw_command(dict(scope,privacy_generation=9),'erase_subject')).status_code==200
    assert post(app,raw_command(scope)).status_code==409
    assert post(app,source_command(scope)).status_code==409


def test_delivery_withdrawal_receipt_and_immutable_ledger(delivery,database):
    app,store=delivery;scope=data()['bindings']['scope']
    assert post(app,raw_command(scope)).status_code==200
    source=source_command(scope);assert post(app,source).status_code==200
    receipt=json.loads(source)['arguments']['receipt']
    withdrawal=raw_command(scope,'withdraw_source',{'source_ref':{k:receipt[k] for k in ('source_id','revision')}})
    assert post(app,withdrawal).status_code==200
    assert post(app,source_command(scope)).status_code==409
    assert post(app,lookup(source),'receipt').json['result']['status']=='applied'
    for sql in ('DELETE FROM knowledge_retained.delivery_receipts','TRUNCATE knowledge_retained.delivery_receipts',"UPDATE knowledge_retained.delivery_receipts SET operation='source'"):
        with psycopg.connect(database) as db,pytest.raises(psycopg.Error):db.execute(sql)
