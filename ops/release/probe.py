"""Synthetic assertions inside an egress-isolated Docker fixture network."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

from redis import Redis
from rq import Queue, Retry, Worker
from rq.job import Callback, Job

sys.path.insert(0,'/app/ops/runtime')
import quarantine_maintenance as quarantine

PILOT = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
STATE = 'fixture:rollback:state'
conn = Redis.from_url(os.environ['REDIS_URL'])
action, url = sys.argv[1:]


def http(path, *, merchant=PILOT, identity=None, extra=None, auth=True, bad_signature=False):
    headers = {'Content-Type':'application/json'}
    if auth: headers['X-API-Key'] = 'fixture-key'
    if merchant: headers['X-Merchant-Id'] = merchant
    data = None
    if identity is not None:
        data = json.dumps({'customer_id':identity,'email':'fixture@example.test','cart_items':[],
                           'cart_total':0,**(extra or {})},sort_keys=True).encode()
        timestamp = str(int(time.time()))
        headers.update({'Idempotency-Key':identity,'X-Wakaru-Timestamp':timestamp,
            'X-Wakaru-Signature':hmac.new(b'fixture-hmac',timestamp.encode()+b'.'+data,hashlib.sha256).hexdigest()})
        if bad_signature: headers['X-Wakaru-Signature'] = '0'*64
    request = urllib.request.Request(url+path,data=data,headers=headers)
    try: response = urllib.request.urlopen(request,timeout=5)
    except urllib.error.HTTPError as error: response = error
    with response:
        raw = response.read()
        try: body = json.loads(raw)
        except ValueError: body = raw.decode()
        return response.status,body


def ready():
    for _ in range(100):
        try:
            status,body = http('/readiness')
            if status == 200:
                assert body == {'analysis':'ok'},body
                return
        except OSError: pass
        time.sleep(.1)
    raise AssertionError('web not ready')


def submit(identity, merchant=PILOT):
    status,body = http('/api/cart-recovery/jobs',identity=identity,merchant=merchant)
    assert status == 202,body
    return body['job_id']


def wait_job(job_id, status):
    for _ in range(150):
        job = Job.fetch(job_id,connection=conn)
        if job.get_status() == status: return job
        time.sleep(.1)
    raise AssertionError((job_id,job.get_status(),job.exc_info))


def save(value): conn.set(STATE,json.dumps(value,sort_keys=True))


def state(): return json.loads(conn.get(STATE))


def data_hash(job_id): return hashlib.sha256(conn.hget('rq:job:'+job_id,'data')).hexdigest()


def non_rate_state():
    return {key:conn.dump(key) for key in conn.scan_iter() if not key.startswith(b'LIMITS:')}


def maintenance_unchanged(value):
    key = ('rq:job:'+value['maintenance']).encode()
    fields = {k.hex():v.hex() for k,v in conn.hgetall(key).items()}
    assert fields == value['maintenance_fields']
    assert conn.ttl(key) == -1
    assert conn.zscore(quarantine.SCHEDULED,value['maintenance']) is None
    assert conn.zscore(quarantine.CANCELED,value['maintenance']) == float('inf')
    assert conn.get(value['receipt']) == value['receipt_body'].encode()
    assert conn.ttl(value['receipt']) == -1
    assert conn.get('zep:graph:merchant_fixture') == b'preserve-personal-memory'
    assert not conn.exists('rq:queue:maintenance')
    for item in Worker.all(connection=conn):
        assert 'maintenance' not in item.queue_names()
    assert not list(conn.scan_iter('rq:scheduler*maintenance*'))


def seed_current():
    ready(); assert conn.dbsize() == 0 or all(k.startswith(b'LIMITS:') or k.startswith(b'rq:') for k in conn.scan_iter())
    done = submit('before-rollback-completed'); wait_job(done,'finished')
    retained = Queue('analyze',connection=conn).enqueue('app.services.cart_recovery_jobs.run_analysis_job',
        {'customer_id':'historic','email':'fixture@example.test','analysis_mode':'legacy_simulation'},
        retry=Retry(max=2),failure_ttl=0,description='synthetic retained job',meta={'merchant_id':PILOT})
    wait_job(retained.id,'failed')
    maintenance = Queue('maintenance',connection=conn).enqueue_at(datetime.now(timezone.utc)-timedelta(days=1),
        quarantine.FUNCTION,result_ttl=0,failure_ttl=86400,
        on_failure=Callback('app.services.maintenance_queue.reseed_chain_on_failure'))
    conn.set('zep:graph:merchant_fixture','preserve-personal-memory')
    backup = {key.hex():base64.b64encode(conn.dump(key)).decode() for key in
        (maintenance.key,quarantine.SCHEDULED,b'zep:graph:merchant_fixture')}
    plan = quarantine.preview(conn,quarantine.digest(maintenance.key))
    result = quarantine.apply_reviewed(conn,plan,producers_stopped=True)
    assert result['status'] == 'quarantined'
    receipt = 'wakaru:retired-maintenance:'+plan['job_ref']
    value = {'completed':done,'completed_data':data_hash(done),'retained':retained.id,
        'retained_data':data_hash(retained.id),'maintenance':maintenance.id,
        'maintenance_fields':{k.hex():v.hex() for k,v in conn.hgetall(maintenance.key).items()},
        'receipt':receipt,'receipt_body':conn.get(receipt).decode(),'plan':plan,'backup':backup}
    save(value);maintenance_unchanged(value)


def stage_pending():
    ready(); value = state(); direct = submit('before-rollback-pending')
    value['pending_direct'] = direct;value['pending_direct_data'] = data_hash(direct)
    value['legacy'] = []
    for mode in ('legacy_simulation',None,'future'):
        payload = {'customer_id':'blocked-fixture','email':'private-fixture@example.test'}
        if mode is not None: payload['analysis_mode'] = mode
        job = Queue('analyze',connection=conn).enqueue('app.services.cart_recovery_jobs.run_analysis_job',
            payload,retry=Retry(max=2),failure_ttl=0,description='synthetic historical payload',meta={'merchant_id':PILOT})
        value['legacy'].append({'id':job.id,'data':data_hash(job.id)})
    save(value)
    assert conn.llen('rq:queue:analyze') == 4


def denied():
    ready(); before = non_rate_state()
    for path in ('jobs','analyze'):
        for merchant, extra in ((OTHER,{}),(None,{}),(OTHER,{'merchant_id':PILOT,'analysis_mode':'direct_v1'})):
            status,body = http('/api/cart-recovery/'+path,merchant=merchant,
                identity='denied-'+path+str(merchant),extra=extra)
            assert status == 410 and body == {'success':False,'error':'legacy_simulation_retired'},body
    assert non_rate_state() == before, 'refusal wrote queue/idempotency state'
    assert http('/api/cart-recovery/jobs',identity='unauthenticated',auth=False)[0] == 401
    assert http('/api/cart-recovery/jobs',identity='bad-signature',bad_signature=True)[0] == 401
    for path in ('graph','simulation','report'):
        assert http('/api/'+path+'/retired')[0] == 404


def rollback_drained():
    ready(); value = state(); wait_job(value['pending_direct'],'finished')
    assert data_hash(value['pending_direct']) == value['pending_direct_data']
    for entry in value['legacy']:
        job = wait_job(entry['id'],'failed')
        assert data_hash(job.id) == entry['data'] and job.retries_left == 0
        assert conn.ttl(job.key) == -1 and conn.zscore('rq:scheduled:analyze',job.id) is None
        status,body = http('/api/cart-recovery/jobs/'+job.id)
        assert status == 200 and body['status'] == 'failed' and 'LegacySimulationRetired' in body['error']
        assert 'private-fixture' not in json.dumps(body) and 'private-fixture' not in job.exc_info
    assert submit('before-rollback-completed') == value['completed']
    assert data_hash(value['completed']) == value['completed_data']
    assert data_hash(value['retained']) == value['retained_data']
    assert Job.fetch(value['retained'],connection=conn).get_status() == 'failed'
    assert http('/api/cart-recovery/jobs/'+value['completed'],merchant=OTHER)[0] == 404
    direct = submit('rollback-direct');wait_job(direct,'finished')
    status,result = http('/api/cart-recovery/jobs/'+direct)
    assert status == 200 and result['result']['buyer_state']['version'] == 'buyer_state_v1'
    status,sync = http('/api/cart-recovery/analyze',identity='rollback-sync')
    assert status == 200 and sync['data']['buyer_state']['version'] == 'buyer_state_v1'
    status,replay = http('/api/cart-recovery/analyze',identity='rollback-sync')
    assert status == 200 and replay['replayed'] and replay['data'] == sync['data']
    assert conn.llen('rq:queue:analyze') == 0
    maintenance_unchanged(value)


def stage_restart():
    value = state();value['restart_direct'] = submit('rollback-restart-pending');save(value)
    assert conn.llen('rq:queue:analyze') == 1


def after_restart():
    ready();value = state();wait_job(value['restart_direct'],'finished')
    assert submit('rollback-restart-pending') == value['restart_direct']
    maintenance_unchanged(value)
    # A process/Redis restart does not make a stale reviewed command reusable.
    try: quarantine.apply_reviewed(conn,value['plan'],producers_stopped=True)
    except quarantine.Refused as error: assert str(error) == 'redis_instance_changed'
    else: raise AssertionError('stale instance review accepted')
    for entry in value['legacy'] + [{'id':value['retained'],'data':value['retained_data']}]:
        job=Job.fetch(entry['id'],connection=conn)
        assert job.get_status()=='failed' and job.retries_left==0 and conn.ttl(job.key)==-1
        assert data_hash(job.id)==entry['data']
    assert conn.llen('rq:queue:analyze') == 0
    assert len(Worker.all(connection=conn,queue=Queue('analyze',connection=conn))) == 1


def restore_backup():
    value = state();restored = Redis.from_url(os.environ['REDIS_URL'].rsplit('/',1)[0]+'/2')
    assert restored.dbsize() == 0
    for key,raw in value['backup'].items(): restored.restore(bytes.fromhex(key),0,base64.b64decode(raw))
    assert restored.zscore(quarantine.SCHEDULED,value['maintenance']) is not None
    try: quarantine.apply_reviewed(restored,value['plan'],producers_stopped=True)
    except quarantine.Refused: pass
    else: raise AssertionError('stale restore review accepted')
    plan = quarantine.preview(restored,value['plan']['job_ref'])
    assert quarantine.apply_reviewed(restored,plan,producers_stopped=True)['status'] == 'quarantined'
    assert restored.zscore(quarantine.SCHEDULED,value['maintenance']) is None
    assert restored.get('zep:graph:merchant_fixture') == b'preserve-personal-memory'


def default_denies():
    ready()
    for merchant in (PILOT,OTHER):
        status,body = http('/api/cart-recovery/jobs',merchant=merchant,identity='default-denial')
        assert status == 410,body


def package_checks():
    for module in ('torch','camel','oasis','openai','zep_cloud'):
        assert importlib.util.find_spec(module) is None,module
    for path in ('backend/app/services/legacy_cart_recovery.py','backend/app/services/maintenance_queue.py','backend/sweep.py'):
        assert not Path('/app',path).exists(),path
    sys.path.insert(0,'/app/backend')
    from app import create_app
    app=create_app()
    assert not any(rule.rule.startswith(('/api/graph','/api/simulation','/api/report')) for rule in app.url_map.iter_rules())
    from cart_recovery.adaptive.promotion import artifact_hash
    assert artifact_hash()==os.environ['ROLLBACK_RELEASE_SHA256']
    from app.services.cart_recovery_workflow import _run_legacy,LegacySimulationRetired
    try: _run_legacy(None,None,None)
    except LegacySimulationRetired: pass
    else: raise AssertionError('workflow fence absent')


def stage_forward():
    ready(); value = state(); job = submit('return-forward-pending')
    value['forward_pending'] = job; value['forward_pending_data'] = data_hash(job)
    save(value)


def after_forward():
    ready(); value = state(); wait_job(value['forward_pending'],'finished')
    assert data_hash(value['forward_pending']) == value['forward_pending_data']
    assert submit('return-forward-pending') == value['forward_pending']
    maintenance_unchanged(value)


actions = {fn.__name__:fn for fn in (seed_current,stage_pending,denied,rollback_drained,
    stage_restart,after_restart,restore_backup,default_denies,package_checks,stage_forward,after_forward)}
actions[action]()
print(json.dumps({'action':action,'passed':True}))
