"""Authenticated privacy transport with real PostgreSQL, unmounted by default."""
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from cart_recovery.domain.privacy_service import AUDIENCE, PREFIX, create_privacy_app
from test_service import data, GRANT as SOURCE_GRANT
from test_lifecycle import seed, save
from test_source_cleanup import export_store, store

GRANT = dict(SOURCE_GRANT, audience=AUDIENCE,
             operations=['POST '+PREFIX+op for op in ('purge_withdrawn','export_retained_page')])


def request_body(scope, op, args):
    return json.dumps(dict(version='knowledge_privacy_request_v1',operation=op,scope=scope,arguments=args),separators=(',',':')).encode()


def post(app, scope, op, args, *, grant=GRANT, raw=None, suffix=''):
    raw = raw if raw is not None else request_body(scope,op,args)
    stamp = str(int(time.time()))
    path = PREFIX+op
    frame = '\n'.join(('integration_service_request_v1',grant['key_id'],grant['audience'],grant['merchant_id'],grant['store_id'],'POST',path,stamp,hashlib.sha256(raw).hexdigest()))
    header = 'Integration v1.'+grant['key_id']+'.'+stamp+'.'+hmac.new(grant['secret'].encode(),frame.encode(),hashlib.sha256).hexdigest()
    return app.test_client().post(path+suffix,data=raw,headers={'Authorization':header})


@pytest.fixture
def privacy_service(export_store):
    store = export_store
    f = data()
    seed(store,f)
    save(store,f)
    store.withdraw_source(f['bindings']['scope'],f['payload']['objects'][0]['sources'][0])
    return create_privacy_app(enabled=True,grants=[GRANT],store=store),store,f


def test_privacy_default_off_and_separate_permissions(export_store):
    scope = data()['bindings']['scope']
    assert post(create_privacy_app(store=export_store),scope,'purge_withdrawn',{'expected_version':0}).status_code == 404
    with pytest.raises(ValueError):
        create_privacy_app(enabled=True,grants=[GRANT])
    with pytest.raises(ValueError):
        create_privacy_app(enabled=True,grants=[dict(GRANT,operations=['POST '+PREFIX+'erase_merchant'])],store=export_store)
    app = create_privacy_app(enabled=True,grants=[dict(GRANT,operations=['POST '+PREFIX+'export_retained_page'])],store=export_store)
    assert post(app,scope,'purge_withdrawn',{'expected_version':0}).status_code == 401
    for op in ['erase_merchant','release_quarantine','bootstrap_empty','source']:
        assert post(app,scope,op,{}).status_code == 404


def test_privacy_exact_version_and_response_binding(privacy_service):
    app,store,f = privacy_service
    scope=f['bindings']['scope']
    assert post(app,scope,'purge_withdrawn',{'expected_version':1}).status_code == 409
    assert len(store.export_subject(scope)['sources']) == 1
    result=post(app,scope,'purge_withdrawn',{'expected_version':2})
    assert result.status_code == 200
    assert result.json['result'] == dict(version=3,removed_sources=1,removed_objects=1,whole_application_complete=False)
    assert result.json['request_sha256'] == hashlib.sha256(request_body(scope,'purge_withdrawn',{'expected_version':2})).hexdigest()
    assert result.json['scope']==scope and result.headers['Cache-Control']=='no-store'
    assert store.export_subject(scope)['sources']==[]
    assert post(app,scope,'purge_withdrawn',{'expected_version':2}).status_code == 409
    assert post(app,scope,'purge_withdrawn',{'expected_version':3}).json['result']['version']==3


@pytest.mark.parametrize('attack',['wrong_store','wrong_subject','wrong_generation','wrong_audience','wrong_secret','missing_version','boolean_version','duplicate','unknown_field','query','body_operation'])
def test_privacy_refuses_unbound_cleanup_without_mutation(privacy_service,attack):
    app,store,f=privacy_service
    scope=dict(f['bindings']['scope']);args={'expected_version':2};kw={}
    if attack=='wrong_store':scope['store_id']='another-store'
    if attack=='wrong_subject':scope['subject_id']='another-subject'
    if attack=='wrong_generation':scope['privacy_generation']+=1
    if attack=='wrong_audience':kw['grant']=dict(GRANT,audience='wakaru-knowledge')
    if attack=='wrong_secret':kw['grant']=dict(GRANT,secret='incorrect-fixture-secret-32-bytes-long')
    if attack=='missing_version':args={}
    if attack=='boolean_version':args={'expected_version':True}
    if attack=='duplicate':kw['raw']=request_body(scope,'purge_withdrawn',args).replace(b'"expected_version":2',b'"expected_version":2,"expected_version":2')
    if attack=='unknown_field':args['extra']=True
    if attack=='query':kw['suffix']='?debug=1'
    if attack=='body_operation':kw['raw']=request_body(scope,'export_retained_page',{})
    assert post(app,scope,'purge_withdrawn',args,**kw).status_code in (400,401,403,409)
    assert len(store.export_subject(f['bindings']['scope'])['sources'])==1
    assert store.current(f['bindings']['scope'])['version']==2


def test_privacy_concurrent_cas_cleanup_has_one_applier(privacy_service):
    app,store,f=privacy_service;scope=f['bindings']['scope']
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:post(app,scope,'purge_withdrawn',{'expected_version':2}).status_code,range(4)))
    assert sorted(results)==[200,409,409,409]
    assert store.current(scope)['version']==3


def test_privacy_export_after_erasure_is_not_completion(privacy_service):
    app,store,f=privacy_service;scope=f['bindings']['scope']
    store.erase_subject(scope)
    args=dict(family='events',limit=1,cursor=None,snapshot=None)
    first=post(app,scope,'export_retained_page',args)
    assert first.status_code==200
    page=first.json['result'];assert page['subject']['erased'] and not page['whole_application_complete']
    args.update(cursor=page['next_cursor'],snapshot=page['snapshot'])
    assert post(app,scope,'export_retained_page',args).status_code==200
    store.erase_merchant(scope['merchant_id'])
    assert post(app,scope,'export_retained_page',args).status_code==409
    assert post(app,scope,'purge_withdrawn',{'expected_version':3}).status_code==409
    store.quarantine()
    args.update(cursor=None,snapshot=None)
    assert post(app,scope,'export_retained_page',args).status_code==409


def test_privacy_database_failure_is_redacted(privacy_service,database):
    app,store,f=privacy_service;scope=f['bindings']['scope']
    with psycopg.connect(database) as db:
        db.execute("CREATE FUNCTION privacy_fixture_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'private fixture detail'; END $$")
        db.execute('CREATE TRIGGER privacy_fixture_failure BEFORE INSERT ON knowledge_retained.events FOR EACH ROW EXECUTE FUNCTION privacy_fixture_failure()')
    reply=post(app,scope,'purge_withdrawn',{'expected_version':2})
    assert reply.status_code==503 and reply.json=={'error':'privacy_unavailable'}
    assert len(store.export_subject(scope)['sources'])==1
