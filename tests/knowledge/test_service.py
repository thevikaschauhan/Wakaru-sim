"""Authenticated service behavior backed by the real disposable PostgreSQL store."""
import base64
import copy
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest

from cart_recovery.domain.service import AUDIENCE, PREFIX, OPERATIONS, create_knowledge_service_app
from cart_recovery.domain.store import KnowledgeStore
from test_lifecycle import fixture, KEY

MERCHANT = '11111111-1111-4111-8111-111111111111'
GRANT = dict(key_id='fixture', secret='fixture-only-knowledge-key-32-bytes-long',
             merchant_id=MERCHANT, store_id='1', audience=AUDIENCE,
             operations=['POST '+PREFIX+op for op in OPERATIONS])


def data():
    value = fixture(merchant=MERCHANT)
    def replace(v):
        if isinstance(v,dict):return {k:replace(x) for k,x in v.items()}
        if isinstance(v,list):return [replace(x) for x in v]
        return '1' if v=='store-a' else v
    return replace(value)


def command(value, op, args=None):
    return dict(version='knowledge_service_request_v1', operation=op,
                scope=value['bindings']['scope'], arguments=args or {})


def signed(raw, op, *, grant=GRANT, stamp=None):
    stamp = str(int(time.time()) if stamp is None else stamp)
    frame = '\n'.join(('integration_service_request_v1',grant['key_id'],grant['audience'],
                      grant['merchant_id'],grant['store_id'],'POST',PREFIX+op,stamp,hashlib.sha256(raw).hexdigest()))
    signature = hmac.new(grant['secret'].encode(),frame.encode(),hashlib.sha256).hexdigest()
    return 'Integration v1.'+grant['key_id']+'.'+stamp+'.'+signature


def post(app, value, op, args=None, *, grant=GRANT, stamp=None):
    raw = json.dumps(command(value,op,args)).encode()
    return app.test_client().post(PREFIX+op, data=raw, headers={'Authorization':signed(raw,op,grant=grant,stamp=stamp)})


@pytest.fixture
def service(database):
    store = KnowledgeStore(database,KEY);store.bootstrap_empty()
    return create_knowledge_service_app(enabled=True, grants=[GRANT], store=store), store


def seed(app, value):
    assert post(app,value,'provision').status_code==200
    for receipt in value['bindings']['sources']:
        raw=json.dumps(receipt['claims']).encode()
        assert post(app,value,'source',{'receipt':receipt,'content_base64':base64.b64encode(raw).decode()}).status_code==200
    for receipt in value['bindings']['entities']:
        assert post(app,value,'entity',{'receipt':receipt}).status_code==200


def save(app,value,expected=0,command_id='save-a'):
    return post(app,value,'save',dict(payload=value['payload'],expected_version=expected,command_id=command_id))


def test_factory_default_off_and_no_operator_routes(database):
    app=create_knowledge_service_app(store=KnowledgeStore(database,KEY))
    assert post(app,data(),'provision').status_code==404
    with pytest.raises(ValueError):create_knowledge_service_app(enabled=True,grants=[GRANT])
    app=create_knowledge_service_app(enabled=True,grants=[GRANT],store=KnowledgeStore(database,KEY))
    for operation in ['bootstrap_empty','restore_fences','release_quarantine','erase_merchant','export_merchant']:
        assert app.test_client().post(PREFIX+operation).status_code==404
    assert post(app,data(),'provision').status_code==409  # cannot lift quarantine


def test_signed_source_save_replay_read_export_and_erasure(service,database):
    app,store=service;value=data();seed(app,value)
    first=save(app,value);assert first.status_code==200
    assert first.json['result']=={'version':1,'replayed':False}
    assert first.headers['Cache-Control']=='no-store'
    raw=json.dumps(command(value,'save',dict(payload=value['payload'],expected_version=0,command_id='save-a'))).encode()
    assert first.json['request_sha256']==hashlib.sha256(raw).hexdigest()
    assert save(app,value).json['result']=={'version':1,'replayed':True}
    restarted=create_knowledge_service_app(enabled=True,grants=[GRANT],store=KnowledgeStore(database,KEY))
    assert post(restarted,value,'current').json['result']['objects']==value['payload']['objects']
    exported=post(app,value,'export_subject').json['result']
    assert exported['payload']==value['payload'] and len(exported['sources'])==1
    assert post(app,value,'erase_subject').status_code==200
    checkpoint=store.retained_checkpoint()
    assert post(app,value,'erase_subject').status_code==200
    assert store.retained_checkpoint()==checkpoint
    for operation in ['current','provision','export_subject']:
        assert post(restarted,value,operation).status_code==409
    assert save(restarted,value).status_code==409
    with psycopg.connect(database) as db:
        for table in ['subjects','sources','catalog','commands']:
            assert db.execute('SELECT count(*) FROM knowledge_data.'+table).fetchone()[0]==0


@pytest.mark.parametrize('attack',['no_auth','bearer','wrong_key','expired','future','audience','operation','tamper'])
def test_authentication_denial_never_provisions(service,attack):
    app,store=service;value=data();raw=json.dumps(command(value,'provision')).encode()
    auth=signed(raw,'provision')
    if attack=='no_auth':auth=''
    elif attack=='bearer':auth='Bearer merchant-jwt'
    elif attack=='wrong_key':auth=signed(raw,'provision',grant={**GRANT,'key_id':'unknown'})
    elif attack=='expired':auth=signed(raw,'provision',stamp=int(time.time())-31)
    elif attack=='future':auth=signed(raw,'provision',stamp=int(time.time())+60)
    elif attack=='audience':auth=signed(raw,'provision',grant={**GRANT,'audience':'wakaru'})
    elif attack=='operation':auth=signed(raw,'current')
    elif attack=='tamper':raw+=b' '
    before=store.retained_checkpoint()
    assert app.test_client().post(PREFIX+'provision',data=raw,headers={'Authorization':auth}).status_code==401
    assert store.retained_checkpoint()==before


@pytest.mark.parametrize('field,value',[('merchant_id','22222222-2222-4222-8222-222222222222'),('store_id','2')])
def test_signed_body_cannot_change_grant_scope(service,field,value):
    app,store=service;v=data();v['bindings']['scope'][field]=value
    before=store.retained_checkpoint();assert post(app,v,'provision').status_code==403
    assert store.retained_checkpoint()==before


def test_read_only_key_cannot_write(service):
    _,store=service;g={**GRANT,'operations':['POST '+PREFIX+'current']}
    app=create_knowledge_service_app(enabled=True,grants=[g],store=store)
    assert post(app,data(),'provision',grant=g).status_code==401
    assert not store.retained_checkpoint()['subjects']


@pytest.mark.parametrize('change',['unknown_field','duplicate','boolean_revision','unknown_arguments','query','oversized'])
def test_malformed_envelope_refused_without_write(service,change):
    app,store=service;c=command(data(),'provision');path=PREFIX+'provision'
    if change=='unknown_field':c['approved']=True
    elif change=='boolean_revision':c['scope']['identity_revision']=True
    elif change=='unknown_arguments':c['arguments']['bootstrap_empty']=True
    raw=json.dumps(c).encode()
    if change=='duplicate':raw=raw[:-1]+b',"operation":"provision"}'
    if change=='query':path+='?scope=other'
    if change=='oversized':raw=b' '*200001
    before=store.retained_checkpoint()
    response=app.test_client().post(path,data=raw,headers={'Authorization':signed(raw,'provision')})
    assert response.status_code in (400,409,413)
    assert store.retained_checkpoint()==before


def test_source_approval_binding_and_withdrawal(service):
    app,store=service;value=data();assert post(app,value,'provision').status_code==200
    receipt=value['bindings']['sources'][0]
    assert post(app,value,'source',dict(receipt=receipt,content_base64=base64.b64encode(b'unapproved').decode())).status_code==409
    assert save(app,value).status_code==409  # caller payload is not an approved receipt
    seed(app,value);assert save(app,value).status_code==200
    ref=dict(source_id=receipt['source_id'],revision=receipt['revision'])
    assert post(app,value,'withdraw_source',{'source_ref':ref}).json['result']['version']==2
    assert post(app,value,'withdraw_source',{'source_ref':ref}).json['result']['version']==2
    assert post(app,value,'current').json['result']['objects']==[]
    assert save(app,value).status_code==409


def test_identity_revision_blocks_old_signed_commands(service):
    app,_=service;value=data();seed(app,value);assert save(app,value).status_code==200
    new={**value['bindings']['scope'],'identity_revision':2,'privacy_generation':2}
    assert post(app,value,'revise_identity',{'new_scope':new}).status_code==200
    assert post(app,value,'current').status_code==409
    assert post(app,value,'erase_subject').status_code==409
    value['bindings']['scope']=new
    assert post(app,value,'current').json['result']['objects']==[]


def test_concurrent_signed_writes_keep_one_cas_winner(service):
    app,_=service;value=data();seed(app,value);barrier=Barrier(2)
    def write(i):barrier.wait();return save(app,value,command_id='save-'+str(i)).status_code
    with ThreadPoolExecutor(2) as pool:assert sorted(pool.map(write,range(2)))==[200,409]
    assert post(app,value,'current').json['result']['version']==1


def test_database_errors_do_not_echo_private_details(service,monkeypatch):
    app,store=service
    def fail(scope):raise psycopg.OperationalError('private-secret-source')
    monkeypatch.setattr(store,'current',fail)
    r=post(app,data(),'current');assert r.status_code==503
    assert r.json=={'error':'knowledge_unavailable'} and b'private-secret' not in r.data


def test_erase_before_first_write_prevents_late_provision(service):
    app,store=service;value=data()
    assert post(app,value,'erase_subject').status_code==200
    before=store.retained_checkpoint()
    assert post(app,value,'provision').status_code==409
    assert store.retained_checkpoint()==before


def test_signed_erase_write_race_never_resurrects_payload(service,database):
    app,_=service;value=data();seed(app,value);assert save(app,value).status_code==200
    barrier=Barrier(2)
    def write():barrier.wait();return save(app,value,expected=1,command_id='late').status_code
    def erase():barrier.wait();return post(app,value,'erase_subject').status_code
    with ThreadPoolExecutor(2) as pool:
        w=pool.submit(write);e=pool.submit(erase)
        assert w.result() in (200,409);assert e.result()==200
    assert post(app,value,'current').status_code==409
    with psycopg.connect(database) as db:
        assert db.execute('SELECT count(*) FROM knowledge_data.subjects').fetchone()[0]==0
