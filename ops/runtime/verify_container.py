"""Exercise a disposable lean web/Redis/worker stack using synthetic data only."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def verify(image):
    name = 'wakaru-lean-' + uuid.uuid4().hex[:12]
    containers = []
    docker('network', 'create', name)
    try:
        redis_name = name + '-redis'
        containers.append(redis_name)
        docker('run', '-d', '--rm', '--network', name, '--name', redis_name, 'redis:8.2.9')
        for _ in range(60):
            try:
                if docker('exec', redis_name, 'redis-cli', 'ping') == 'PONG':
                    break
            except subprocess.CalledProcessError:
                pass
            time.sleep(.5)
        else:
            raise RuntimeError('fixture Redis failed to start')
        settings = ['SECRET_KEY=fixture-secret', 'WAKARU_API_KEY=fixture-api',
                    'WAKARU_INTERNAL_SECRET=fixture-internal', 'TYPESAFE_RECOVERY_MODE=off',
                    'REDIS_URL=redis://' + redis_name + ':6379/0', 'SENTRY_DSN=']
        for role, command in [('worker', ['python', 'worker.py']), ('web', [])]:
            target = name + '-' + role
            containers.append(target)
            args = ['run', '-d', '--rm', '--network', name, '--name', target]
            for value in settings:
                args.extend(['-e', value])
            docker(*args, image, *command)
        root = Path(__file__).resolve().parents[2]
        names = {line for line in (root/'ops/runtime/lean-files.txt').read_text().splitlines() if line and not line.startswith('#')}
        hashes = {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}
        expected = hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        docker('exec', name+'-web', 'mkdir','-p','/tmp/verification/tests','/tmp/verification/contracts/integrations/c8')
        docker('cp',str(root/'tests/test_bounded_context.py'),name+'-web:/tmp/verification/tests/')
        docker('cp',str(root/'contracts/integrations/c8/fixture.json'),name+'-web:/tmp/verification/contracts/integrations/c8/')
        probe = r'''
import hashlib, hmac, importlib.util, json, sys, tempfile, time, urllib.request, urllib.error
from pathlib import Path
for module in ['torch', 'camel', 'oasis', 'openai', 'zep_cloud']:
    assert importlib.util.find_spec(module) is None, module
assert not Path('/app/backend/app/services/legacy_cart_recovery.py').exists()
assert not Path('/app/backend/scripts').exists()
assert not list(Path('/app').rglob('test_*.py'))
from cart_recovery.adaptive import promotion
assert promotion.artifact_hash() == sys.argv[1], 'source/image release binding differs'
base = 'http://127.0.0.1:5001'
for _ in range(100):
    try:
        with urllib.request.urlopen(base+'/readiness', timeout=1) as response:
            assert json.load(response) == {'analysis':'ok'}
        break
    except OSError:
        time.sleep(.2)
else:
    raise AssertionError('HTTP readiness failed')
for path in ['/api/internal/v2/recovery/evaluate','/api/internal/v2/recovery/check-draft']:
    req = urllib.request.Request(base+path,data=b'{}',headers={'X-API-Key':'fixture-api','Content-Type':'application/json'})
    try:
        urllib.request.urlopen(req,timeout=3)
    except urllib.error.HTTPError as exc:
        assert exc.code == 503, (path,exc.code)
    else:
        raise AssertionError('adaptive route enabled by default')
# Opt-in integration factory and real SQLite lifecycle inside the built image.
sys.path.insert(0,'/tmp/verification/tests')
from test_bounded_context import fixture
from cart_recovery.integration_privacy import PrivacyStore
from cart_recovery.integration_worker_health import create_worker_service_app
value, bound = fixture()
grant = dict(key_id='fixture', secret='fixture-only-'+32*'x', merchant_id=bound['merchant'],store_id=bound['store'],audience='wakaru',operations=['POST /internal/integrations/analyze'])
raw = json.dumps(value).encode()
path = '/internal/integrations/analyze'
ts = str(int(time.time()))
framed = '\n'.join(('integration_service_request_v1',grant['key_id'],'wakaru',bound['merchant'],bound['store'],'POST',path,ts,hashlib.sha256(raw).hexdigest()))
auth = 'Integration v1.fixture.'+ts+'.'+hmac.new(grant['secret'].encode(),framed.encode(),hashlib.sha256).hexdigest()
with tempfile.TemporaryDirectory() as directory:
    store = PrivacyStore(Path(directory)/'privacy.sqlite')
    assert create_worker_service_app().test_client().post(path).status_code == 404
    app = create_worker_service_app(enabled=True,grants=[grant],store=store)
    client = app.test_client()
    headers = {'Authorization':auth,'Content-Type':'application/json'}
    assert client.post(path,data=raw).status_code == 401
    response = client.post(path,data=raw,headers=headers)
    assert response.status_code == 200, response.json
    assert response.json['plan']['action'] == 'review'
    task = dict(version='integration_privacy_task_v1',merchant_id=bound['merchant'],store_id=bound['store'],request_id='combined-erase',subject_key=hashlib.sha256(bound['subject'].encode()).hexdigest(),privacy_generation=2,operation='erase',scope='subject',source_keys=value['source_keys'],inventory_revision='integration_c5_v1',live_ingestion_enabled=False,email_use_enabled=False)
    store.apply(bound['merchant'],bound['store'],json.dumps(task),now=time.time())
    restarted = create_worker_service_app(enabled=True,grants=[grant],store=PrivacyStore(Path(directory)/'privacy.sqlite')).test_client()
    assert restarted.post(path,data=raw,headers=headers).status_code == 422
print(json.dumps({'adaptive_default_off':True,'release_binding':True,'integration_authenticated_sqlite_erasure':True}))
body = json.dumps({'customer_id':'fixture', 'email':'fixture@example.com', 'event_id':'fixture-episode',
    'evidence_events':[{'event_id':'e1','event_name':'alert_displayed','occurred_at':'2026-09-15T03:58:41.885Z','alert_type':'PAYMENT_ERROR'}]}).encode()
ts = str(int(time.time()))
sig = hmac.new(b'fixture-internal', ts.encode()+b'.'+body, hashlib.sha256).hexdigest()
request = urllib.request.Request(base+'/api/cart-recovery/jobs', data=body, headers={
    'Content-Type':'application/json', 'X-API-Key':'fixture-api',
    'X-Wakaru-Timestamp':ts, 'X-Wakaru-Signature':sig})
with urllib.request.urlopen(request, timeout=5) as response:
    assert response.status == 202
    accepted = json.load(response)
for _ in range(100):
    request = urllib.request.Request(base+accepted['status_url'], headers={'X-API-Key':'fixture-api'})
    with urllib.request.urlopen(request, timeout=3) as response:
        payload = json.load(response)
    if payload.get('result'):
        assert payload['result']['buyer_state']['observed_friction'] == [{'code':'PAYMENT_ERROR','evidence_ids':['e1']}], payload
        assert payload['result']['reason_category'] == 'unknown'
        print(json.dumps({'signed_http':True,'real_redis_worker':True,'lean_dependencies':True,'result':'passed'}))
        break
    assert payload.get('status') != 'failed', payload
    time.sleep(.2)
else:
    raise AssertionError('worker did not finish fixture job')
# Inject historical payloads directly into fixture Redis, bypassing the new
# web admission gate, to verify the built worker fences old producers too.
import os
from redis import Redis
from rq import Queue, Retry
connection=Redis.from_url(os.environ['REDIS_URL'])
queue=Queue('analyze',connection=connection)
for legacy in ({'analysis_mode':'legacy_simulation'}, {}):
    cart={'customer_id':'fixture-old','email':'fixture-old@example.test',**legacy}
    job=queue.enqueue('app.services.cart_recovery_jobs.run_analysis_job',cart,
        retry=Retry(max=2),failure_ttl=0,description='retirement fixture')
    original=connection.hget(job.key,'data')
    for _ in range(100):
        request=urllib.request.Request(base+'/api/cart-recovery/jobs/'+job.id,headers={'X-API-Key':'fixture-api'})
        with urllib.request.urlopen(request,timeout=3) as response:old=json.load(response)
        if old['status']=='failed':break
        time.sleep(.2)
    else:raise AssertionError('historical job did not fail closed')
    assert 'LegacySimulationRetired' in old['error'],old
    assert connection.hget(job.key,'data')==original
    assert connection.hget(job.key,'retries_left')==b'0'
    assert connection.ttl(job.key)==-1
    assert connection.zscore('rq:scheduled:analyze',job.id) is None
print(json.dumps({'packaged_retirement_worker':True,'historical_payloads_preserved':True,'automatic_retry_disabled':True}))
'''
        print(docker('exec', name+'-web', 'python', '-c', probe, expected))
        print(json.dumps({'image': image, 'image_bytes': int(docker('image', 'inspect', image, '--format', '{{.Size}}'))}))
    except Exception:
        for container in containers:
            subprocess.run(['docker', 'logs', '--tail', '30', container], check=False)
        raise
    finally:
        for container in reversed(containers):
            subprocess.run(['docker', 'rm', '-f', container], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        docker('network', 'rm', name)


if __name__ == '__main__':
    verify(sys.argv[1])
