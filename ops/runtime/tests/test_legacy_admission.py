"""Signed HTTP and persisted RQ retirement behavior on disposable Redis."""
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest
from rq import Queue, Retry
from rq.job import Job

from test_inventory_legacy import database, dump  # noqa: F401

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT, ROOT/'backend'):
    sys.path.insert(0, str(path))
MERCHANT = '11111111-1111-4111-8111-111111111111'
BODY = {'customer_id':'fixture', 'email':'private@example.test', 'cart_items':[], 'cart_total':0}


@pytest.fixture
def client(database, monkeypatch):
    _, url = database
    for key, value in {'SECRET_KEY':'fixture-secret', 'WAKARU_API_KEY':'fixture-key',
        'WAKARU_INTERNAL_SECRET':'fixture-hmac', 'REDIS_URL':url, 'SENTRY_DSN':'',
        'RECOVERY_ANALYSIS_MODE':'direct_v1', 'RECOVERY_DIRECT_MERCHANT_IDS':'',
        'TYPESAFE_RECOVERY_MODE':'off', 'TYPESAFE_RECOVERY_MERCHANTS':''}.items():
        monkeypatch.setenv(key, value)
    from app import create_app
    app = create_app();app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    return app.test_client()


def post(client, route, body=None, merchant=MERCHANT):
    data=json.dumps(BODY if body is None else body)
    ts=str(int(time.time()))
    signature=hmac.new(b'fixture-hmac',(ts+'.'+data).encode(),hashlib.sha256).hexdigest()
    return client.post('/api/cart-recovery/'+route,data=data,headers={
        'X-API-Key':'fixture-key','X-Merchant-Id':merchant,'Content-Type':'application/json',
        'X-Wakaru-Timestamp':ts,'X-Wakaru-Signature':signature,'Idempotency-Key':'fixture-admission'})


@pytest.mark.parametrize('route',['jobs','analyze'])
@pytest.mark.parametrize('mode',[None,'legacy_simulation'])
def test_legacy_http_rejected_before_queue_or_idempotency(client,database,monkeypatch,route,mode):
    if mode is None:monkeypatch.delenv('RECOVERY_ANALYSIS_MODE')
    else:monkeypatch.setenv('RECOVERY_ANALYSIS_MODE',mode)
    import app.api.cart_recovery as api
    monkeypatch.setattr(api,'run_cart_recovery',lambda *a,**k:pytest.fail('workflow entered'))
    before=dump(database[0])
    response=post(client,route,{**BODY,'analysis_mode':'direct_v1','ontology_hint':{'code':'UNKNOWN_ABANDONMENT'}})
    assert response.status_code==410,response.json
    assert response.json['error']=='legacy_simulation_retired'
    # Authenticated attempts still consume their normal rate-limit budget.
    after=dump(database[0])
    assert {k:v for k,v in after.items() if not k.startswith(b'LIMITS:')}==before


def run_worker(url):
    import os
    code='''
import importlib.abc,sys
class RejectLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.startswith(('app.services.legacy_cart_recovery','app.services.simulation')) or fullname.split('.')[0] in {'zep_cloud','openai','torch','camel','oasis'}:
            raise AssertionError('legacy/provider import attempted')
sys.meta_path.insert(0,RejectLegacy())
from redis import Redis
from rq import Worker
Worker(['analyze'],connection=Redis.from_url(sys.argv[1]),log_job_description=False).work(burst=True,with_scheduler=False)
'''
    return subprocess.run([sys.executable,'-c',code,url],cwd=ROOT,env={**os.environ,
        'PYTHONPATH':str(ROOT/'backend')+':'+str(ROOT),'PYTHONDONTWRITEBYTECODE':'1'},
        capture_output=True,text=True,timeout=30)


def test_supported_sync_analysis_remains_direct(client,database):
    response=post(client,'analyze')
    assert response.status_code==200,response.json
    assert response.json['data']['buyer_state']['version']=='buyer_state_v1'
    replay=post(client,'analyze')
    assert replay.json['replayed'] is True
    assert replay.json['data']==response.json['data']
    assert database[0].llen('rq:queue:analyze')==0


def test_allowlist_direct_job_finishes_in_real_worker_and_other_merchant_is_blocked(client,database,monkeypatch):
    monkeypatch.setenv('RECOVERY_ANALYSIS_MODE','legacy_simulation')
    monkeypatch.setenv('RECOVERY_DIRECT_MERCHANT_IDS',MERCHANT)
    denied=post(client,'jobs',{**BODY,'merchant_id':MERCHANT},merchant='22222222-2222-4222-8222-222222222222')
    assert denied.status_code==410
    response=post(client,'jobs');assert response.status_code==202,response.json
    replay=post(client,'jobs');assert replay.json['job_id']==response.json['job_id']
    run=run_worker(database[1]);assert run.returncode==0,run.stdout+run.stderr
    result=client.get(response.json['status_url'],headers={'X-API-Key':'fixture-key','X-Merchant-Id':MERCHANT})
    assert result.json['status']=='finished',result.json
    assert result.json['result']['buyer_state']['version']=='buyer_state_v1'
    assert client.get(response.json['status_url'],headers={'X-API-Key':'fixture-key'}).status_code==404


@pytest.mark.parametrize('mode',['legacy_simulation',None,'future'])
def test_old_jobs_fail_without_legacy_imports_retries_or_payload_loss(client,database,mode):
    conn,url=database
    payload=dict(BODY)
    if mode is not None:payload['analysis_mode']=mode
    job=Queue('analyze',connection=conn).enqueue('app.services.cart_recovery_jobs.run_analysis_job',payload,
        meta={'merchant_id':MERCHANT},description='legacy fixture',retry=Retry(max=2),failure_ttl=0)
    original=conn.hget(job.key,'data')
    run=run_worker(url);assert run.returncode==0,run.stdout+run.stderr
    restored=Job.fetch(job.id,connection=conn)
    assert restored.get_status()=='failed'
    assert restored.retries_left==0
    assert conn.hget(job.key,'data')==original
    assert conn.ttl(job.key)==-1
    assert conn.llen('rq:queue:analyze')==0
    assert conn.zcard('rq:scheduled:analyze')==0
    assert 'private@example.test' not in restored.exc_info
    assert 'LegacySimulationRetired' in restored.exc_info
    polled=client.get('/api/cart-recovery/jobs/'+job.id,headers={'X-API-Key':'fixture-key','X-Merchant-Id':MERCHANT})
    assert polled.json['status']=='failed'
    assert 'private@example.test' not in json.dumps(polled.json)
