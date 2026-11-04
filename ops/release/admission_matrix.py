"""Exercise real web/worker startup and late policy drift without network access."""
import argparse
import json
from pathlib import Path
import subprocess

PILOT = '11111111-1111-4111-8111-111111111111'
BASE = {'SECRET_KEY':'fixture-secret','WAKARU_API_KEY':'fixture-key',
        'WAKARU_INTERNAL_SECRET':'fixture-hmac','RECOVERY_ANALYSIS_MODE':'legacy_simulation',
        'TYPESAFE_RECOVERY_MODE':'off','SENTRY_DSN':'','PYTHONDONTWRITEBYTECODE':'1'}
LATE_DRIFT = r'''
import hashlib,hmac,json,os,time
from unittest.mock import patch
from app import create_app
app=create_app(); app.config.update(TESTING=True,RATELIMIT_ENABLED=False)
client=app.test_client()
body=json.dumps({'customer_id':'fixture','email':'fixture@example.test'}).encode()
ts=str(int(time.time()))
headers={'X-API-Key':'fixture-key','X-Merchant-Id':'11111111-1111-4111-8111-111111111111',
 'X-Wakaru-Timestamp':ts,'X-Wakaru-Signature':hmac.new(b'fixture-hmac',ts.encode()+b'.'+body,hashlib.sha256).hexdigest(),'Content-Type':'application/json'}
for key,value in [('RECOVERY_ANALYSIS_MODE','direct_v1'),('RECOVERY_DIRECT_MERCHANT_IDS','*')]:
 os.environ['RECOVERY_ANALYSIS_MODE']='legacy_simulation'
 os.environ['RECOVERY_DIRECT_MERCHANT_IDS']='11111111-1111-4111-8111-111111111111'
 os.environ[key]=value
 with patch('socket.socket.connect',side_effect=AssertionError('unexpected network')):
  for route in ('/api/cart-recovery/jobs','/api/cart-recovery/analyze'):
   r=client.post(route,data=body,headers=headers)
   assert r.status_code==503,(r.status_code,r.json)
   assert r.json=={'success':False,'error':'Analysis mode unavailable'},r.json
  r=client.get('/readiness');assert r.status_code==503 and r.json=={'analysis':'unavailable'},r.json
print('late configuration drift denied before network access')
'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--artifact',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    build=json.loads((args.artifact/'build.json').read_text());image=build['tag']
    state=json.loads(subprocess.check_output(['docker','image','inspect',image]))[0]
    assert state['Id']==build['forward']['Id']
    results=[]
    invalid=[('global_direct',{'RECOVERY_ANALYSIS_MODE':'direct_v1'}),
             ('unknown_mode',{'RECOVERY_ANALYSIS_MODE':'future'}),
             ('empty_mode',{'RECOVERY_ANALYSIS_MODE':''}),
             ('wildcard',{'RECOVERY_DIRECT_MERCHANT_IDS':'*'}),
             ('malformed',{'RECOVERY_DIRECT_MERCHANT_IDS':'not-a-uuid'}),
             ('nil',{'RECOVERY_DIRECT_MERCHANT_IDS':'00000000-0000-0000-0000-000000000000'}),
             ('noncanonical',{'RECOVERY_DIRECT_MERCHANT_IDS':'ABCDEFAB-1111-4111-8111-111111111111'}),
             ('mixed',{'RECOVERY_DIRECT_MERCHANT_IDS':PILOT+',*'})]
    for case,extra in invalid:
        for role,command in [('web',[]),('worker',['python','worker.py'])]:
            flags=['docker','run','--rm','--pull=never','--network=none','--read-only','--tmpfs','/tmp:rw,nosuid,size=64m']
            for k,v in {**BASE,**extra}.items(): flags+=['-e',k+'='+v]
            run=subprocess.run(flags+[image]+command,capture_output=True,timeout=30)
            output=run.stdout+run.stderr
            assert run.returncode!=0 and (b'retirement requires' in output or b'allowlist requires' in output),(case,role,output)
            results.append({'case':case,'role':role,'passed':True})
    flags=['docker','run','--rm','--pull=never','--network=none','--read-only','--tmpfs','/tmp:rw,nosuid,size=64m','--entrypoint','python']
    for k,v in {**BASE,'RECOVERY_DIRECT_MERCHANT_IDS':PILOT}.items():flags+=['-e',k+'='+v]
    run=subprocess.run(flags+[image,'-c',LATE_DRIFT],capture_output=True,timeout=30)
    assert run.returncode==0,(run.stdout+run.stderr).decode()
    results.append({'case':'runtime_policy_drift_before_network','passed':True})
    report={'image':state['Id'],'passed':True,'checks':results,'production_access':False}
    args.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':True,'checks':len(results)}))


if __name__=='__main__': main()
