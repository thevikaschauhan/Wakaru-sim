"""Reconcile only a reviewed obsolete job, retaining all evidence."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from rq import Queue
from rq.job import Callback

from test_inventory_legacy import database, dump  # noqa: F401

OPS=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(OPS))
import quarantine_maintenance as quarantine


def seed(conn):
    queue=Queue('maintenance',connection=conn)
    job=queue.enqueue_at(datetime.now(timezone.utc)-timedelta(days=1),quarantine.FUNCTION,
        result_ttl=0,failure_ttl=86400,on_failure=Callback('app.services.maintenance_queue.reseed_chain_on_failure'))
    Queue('analyze',connection=conn).enqueue('app.services.cart_recovery_jobs.run_analysis_job',
        {'analysis_mode':'direct_v1'},job_id='unrelated-direct',description='fixture')
    conn.set('zep:graph:merchant_fixture','preserve-merchant-memory')
    return job,quarantine.digest(job.key)


def test_preview_cli_and_quarantine_preserve_job_callbacks_and_other_keys(database):
    conn,url=database;job,ref=seed(conn)
    before=dump(conn);fields=conn.hgetall(job.key)
    result=subprocess.run([sys.executable,str(OPS/'quarantine_maintenance.py'),'--job-ref',ref],
        env={**os.environ,'REDIS_URL':url},capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    plan=json.loads(result.stdout)
    assert dump(conn)==before
    assert plan['apply_performed'] is False
    assert job.id not in result.stdout and 'reseed_chain' not in result.stdout
    with pytest.raises(quarantine.Refused,match='confirmation_required'):
        quarantine.apply_reviewed(conn,plan)
    assert dump(conn)==before
    assert quarantine.apply_reviewed(conn,plan,producers_stopped=True)['status']=='quarantined'
    after=conn.hgetall(job.key)
    assert after=={**fields,b'status':b'canceled'}
    assert conn.pttl(job.key)==-1
    assert conn.zscore('rq:scheduled:maintenance',job.id) is None
    assert conn.zscore('rq:canceled:maintenance',job.id)==float('inf')
    for key,value in before.items():
        if key not in (job.key,b'rq:scheduled:maintenance'):
            assert conn.dump(key)==value,key
    once=dump(conn)
    assert quarantine.apply_reviewed(conn,plan,producers_stopped=True)['status']=='already_quarantined'
    assert dump(conn)==once


@pytest.mark.parametrize('change',['payload','status','score','expiry','marker','worker','dependency','other_queue','wrong_registry','callable','arguments','infinite_score','future_score'])
def test_changed_or_ambiguous_state_refuses_without_writes(database,change):
    conn,_=database;job,ref=seed(conn);plan=quarantine.preview(conn,ref)
    if change=='payload':conn.hset(job.key,'data',b'invalid')
    elif change=='status':conn.hset(job.key,'status','started')
    elif change=='score':conn.zadd('rq:scheduled:maintenance',{job.id:1})
    elif change=='expiry':conn.expire(job.key,3600)
    elif change=='marker':conn.set('zep:sweep:next',job.id)
    elif change=='worker':conn.hset('rq:worker:fixture','queues','maintenance')
    elif change=='dependency':conn.sadd(job.key+b':dependents','dependent-job')
    elif change=='other_queue':conn.rpush('rq:queue:unexpected',job.id)
    elif change=='wrong_registry':conn.set('rq:canceled:maintenance','wrong-type')
    elif change=='infinite_score':conn.zadd('rq:scheduled:maintenance',{job.id:float('-inf')})
    elif change=='future_score':conn.zadd('rq:scheduled:maintenance',{job.id:9999999999})
    else:
        import pickle,zlib
        call=('unrelated.callable' if change=='callable' else quarantine.FUNCTION,None,('unexpected',) if change=='arguments' else (),{})
        conn.hset(job.key,'data',zlib.compress(pickle.dumps(call)))
    before=dump(conn)
    with pytest.raises(quarantine.Refused):quarantine.apply_reviewed(conn,plan,producers_stopped=True)
    assert dump(conn)==before


def test_stale_and_wrong_instance_plans_refuse(database):
    conn,_=database;_,ref=seed(conn);plan=quarantine.preview(conn,ref);before=dump(conn)
    for changed in ({**plan,'prepared_at':1},{**plan,'redis_run_id':'other-instance'},
                    {**plan,'action':'delete'},{**plan,'job_ref':'0'*64}):
        with pytest.raises(quarantine.Refused):quarantine.apply_reviewed(conn,changed,producers_stopped=True)
        assert dump(conn)==before


def test_race_after_review_aborts_transaction(database,monkeypatch):
    conn,_=database;job,ref=seed(conn);plan=quarantine.preview(conn,ref)
    original=quarantine.inspect;calls=0
    def race(*args,**kwargs):
        nonlocal calls
        result=original(*args,**kwargs);calls+=1
        if calls==2:conn.hset(job.key,'status','started')
        return result
    monkeypatch.setattr(quarantine,'inspect',race)
    with pytest.raises(quarantine.Refused,match='concurrent_change'):
        quarantine.apply_reviewed(conn,plan,producers_stopped=True)
    assert conn.hget(job.key,'status')==b'started'
    assert conn.zscore('rq:scheduled:maintenance',job.id) is not None
    assert conn.zscore('rq:canceled:maintenance',job.id) is None
    assert not conn.exists('wakaru:retired-maintenance:'+ref)


def test_lost_commit_reply_reports_unknown_then_receipt_reconciles(database,monkeypatch,tmp_path,capsys):
    import redis
    conn,url=database;job,ref=seed(conn);plan=quarantine.preview(conn,ref)
    path=tmp_path/'plan.json';path.write_text(json.dumps(plan))
    pipeline=conn.pipeline
    def lost_reply(*args,**kwargs):
        pipe=pipeline(*args,**kwargs);execute=pipe.execute
        def commit_then_disconnect(*a,**k):
            execute(*a,**k)
            raise redis.ConnectionError('fixture lost reply')
        pipe.execute=commit_then_disconnect
        return pipe
    monkeypatch.setattr(conn,'pipeline',lost_reply)
    monkeypatch.setattr(redis.Redis,'from_url',lambda *a,**k:conn)
    monkeypatch.setenv('REDIS_URL',url)
    monkeypatch.setattr(sys,'argv',['quarantine','--apply-reviewed-plan',str(path),'--confirm-producers-stopped'])
    assert quarantine.main()==2
    assert json.loads(capsys.readouterr().out)['status']=='outcome_unknown'
    assert conn.hget(job.key,'status')==b'canceled'
    committed=dump(conn)
    assert quarantine.apply_reviewed(conn,plan,producers_stopped=True)['status']=='already_quarantined'
    assert dump(conn)==committed
