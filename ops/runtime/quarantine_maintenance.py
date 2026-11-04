"""Preview one obsolete sweep occurrence; explicit reviewed apply preserves it.

No RQ imports, job execution, callback loading, DEL, expiry or provider calls.
Production use is a separate authorized operation after producers are fenced.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from redis.exceptions import WatchError
from inventory_legacy import MAX_BYTES, Reader, REGISTRIES, primitive_pickle

EXPECTED_JOB_REF = '642519ca84168a24784573376fc6395b8f02178e2be76b422bcedcbdd3f4a1f0'
FUNCTION = 'app.services.maintenance_queue.run_sweep_occurrence'
SCHEDULED = b'rq:scheduled:maintenance'
CANCELED = b'rq:canceled:maintenance'
MARKERS = (b'zep:sweep:lock', b'zep:sweep:next', b'rq:scheduler-lock:maintenance')


class Refused(RuntimeError):
    """Fixed error codes only; never expose job data or connection credentials."""


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def fields_digest(fields):
    return digest(encoded({k.hex():v.hex() for k,v in fields.items() if k != b'status'}))


def read_fields(conn, key):
    names = conn.hkeys(key)
    if len(names) > 128 or sum(conn.hstrlen(key, name) for name in names) > MAX_BYTES:
        raise Refused('job_size_limit')
    fields = conn.hgetall(key)
    if sum(len(k)+len(v) for k,v in fields.items()) > MAX_BYTES:
        raise Refused('job_size_limit')
    return fields


def inspect(conn, job_ref=EXPECTED_JOB_REF):
    if not isinstance(job_ref, str) or not re.fullmatch('[0-9a-f]{64}', job_ref):
        raise Refused('invalid_reference')
    reader = Reader(conn)
    keys = reader.scan()
    matches = [k for k in keys if k.startswith(b'rq:job:') and digest(k) == job_ref]
    if len(matches) != 1 or conn.type(matches[0]) != b'hash':
        raise Refused('exact_job_not_found')
    key = matches[0]; job_id = key[len(b'rq:job:'):]
    fields = read_fields(conn, key)
    if fields.get(b'origin') != b'maintenance' or fields.get(b'status') != b'scheduled':
        raise Refused('job_not_scheduled_maintenance')
    try:
        raw = fields[b'data']
        call = primitive_pickle(raw, compressed=not raw.startswith(b'\x80'))
        if call != (FUNCTION, None, (), {}):
            raise Refused('unexpected_callable_or_arguments')
    except Refused:
        raise
    except Exception:
        raise Refused('unreadable_callable') from None
    if fields.get(b'retries_left', b'0') != b'0':
        raise Refused('retry_requires_review')
    if any(conn.exists(k) for k in MARKERS):
        raise Refused('scheduler_marker_present')
    memberships = []
    for name in keys:
        if name.startswith(b'rq:queue:') or any(name.startswith(p) for p in REGISTRIES):
            kind = conn.type(name)
            if kind not in (b'list', b'zset'):
                raise Refused('unexpected_registry_type')
            if job_id in reader.members(name, kind):
                memberships.append(name)
        if name.startswith(b'rq:worker:') and conn.type(name) == b'hash':
            queues = reader.blob(name, 'queues') or b''
            if b'maintenance' in queues:
                raise Refused('maintenance_worker_present')
        if name.startswith(b'rq:scheduler') and b'maintenance' in name:
            raise Refused('maintenance_scheduler_present')
        if name != key and job_id in name:
            raise Refused('related_artifacts_require_review')
    if memberships != [SCHEDULED]:
        raise Refused('unexpected_memberships')
    if conn.type(CANCELED) not in (b'none', b'zset'):
        raise Refused('unexpected_canceled_registry_type')
    score = conn.zscore(SCHEDULED, job_id)
    if score is None or not math.isfinite(score) or score < 0 or score > time.time():
        raise Refused('not_overdue')
    plan = {'version':'obsolete_maintenance_quarantine_v1', 'action':'preserve_and_cancel',
            'job_ref':job_ref, 'retained_fields_sha256':fields_digest(fields),
            'scheduled_score':score, 'redis_run_id':conn.info('server')['run_id'],
            'redis_database':int(conn.connection_pool.connection_kwargs.get('db', 0)),
            'original_pttl':conn.pttl(key)}
    # An expiring job cannot be promised retained without changing its policy.
    if plan['original_pttl'] != -1:
        raise Refused('expiring_job_requires_review')
    watched = set(keys) | set(MARKERS) | {key, SCHEDULED, CANCELED, b'rq:workers',
        b'rq:workers:maintenance', b'rq:queue:maintenance'}
    return plan, key, watched


def preview(conn, job_ref=EXPECTED_JOB_REF):
    plan, _, _ = inspect(conn, job_ref)
    return {**plan, 'prepared_at':int(time.time()), 'apply_performed':False}


def apply_reviewed(conn, reviewed, *, producers_stopped=False):
    if producers_stopped is not True:
        raise Refused('producer_fence_confirmation_required')
    prepared = reviewed.get('prepared_at')
    if not isinstance(prepared, int) or not 0 <= time.time()-prepared <= 900:
        raise Refused('review_expired')
    ref = reviewed.get('job_ref')
    if not isinstance(ref, str) or not re.fullmatch('[0-9a-f]{64}', ref):
        raise Refused('invalid_reference')
    if (reviewed.get('redis_run_id') != conn.info('server')['run_id']
            or reviewed.get('redis_database') != int(conn.connection_pool.connection_kwargs.get('db',0))):
        raise Refused('redis_instance_changed')
    receipt = ('wakaru:retired-maintenance:'+ref).encode()
    expected_receipt = encoded({'review_sha256':digest(encoded(reviewed)), 'review':reviewed})
    if conn.exists(receipt):
        if conn.get(receipt) != expected_receipt:
            raise Refused('different_review_already_applied')
        # A duplicate call makes no writes. A changed record is not called safe.
        keys = Reader(conn).scan()
        matches = [k for k in keys if k.startswith(b'rq:job:') and digest(k)==ref]
        if len(matches)!=1:
            raise Refused('quarantined_job_missing')
        key=matches[0];job_id=key[len(b'rq:job:'):];fields=read_fields(conn,key)
        if (fields.get(b'status')!=b'canceled' or fields_digest(fields)!=reviewed['retained_fields_sha256']
                or conn.zscore(SCHEDULED,job_id) is not None or conn.zscore(CANCELED,job_id)!=float('inf')):
            raise Refused('quarantine_state_changed')
        return {'status':'already_quarantined','job_ref':ref,'payload_preserved':True}
    _, key, watched = inspect(conn, ref)
    job_id = key[len(b'rq:job:'):]
    with conn.pipeline() as pipe:
        try:
            pipe.watch(*(watched | {receipt}))
            current, current_key, current_watch = inspect(conn, ref)
            expected = {**current, 'prepared_at':prepared, 'apply_performed':False}
            if current_key != key or not current_watch.issubset(watched) or reviewed != expected or conn.exists(receipt):
                raise Refused('state_changed_since_review')
            pipe.multi()
            # Transfer eligibility, preserve every job field except its status.
            # No worker, success/failure callback or scheduler is invoked.
            pipe.zrem(SCHEDULED, job_id)
            pipe.zadd(CANCELED, {job_id:float('inf')})
            pipe.hset(key, 'status', 'canceled')
            pipe.set(receipt, expected_receipt)
            pipe.execute()
        except WatchError:
            raise Refused('concurrent_change_no_apply') from None
    return {'status':'quarantined','job_ref':ref,'payload_preserved':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-ref',default=EXPECTED_JOB_REF)
    parser.add_argument('--apply-reviewed-plan',type=Path)
    parser.add_argument('--confirm-producers-stopped',action='store_true')
    args=parser.parse_args()
    import redis
    try:
        with redis.Redis.from_url(os.environ['REDIS_URL'],socket_connect_timeout=5,socket_timeout=5) as conn:
            if args.apply_reviewed_plan:
                if args.apply_reviewed_plan.stat().st_size>16384:raise Refused('plan_size_limit')
                result=apply_reviewed(conn,json.loads(args.apply_reviewed_plan.read_text()),producers_stopped=args.confirm_producers_stopped)
            else:
                result=preview(conn,args.job_ref)
        print(json.dumps(result,sort_keys=True));return 0
    except Exception as exc:
        # A lost EXEC reply can follow a committed transaction. Never label
        # that as "no apply" or blindly retry; reconcile the immutable receipt.
        status = 'outcome_unknown' if args.apply_reviewed_plan and not isinstance(exc,Refused) else 'refused'
        print(json.dumps({'status':status,'error':str(exc) if isinstance(exc,Refused) else type(exc).__name__}));return 2


if __name__=='__main__':raise SystemExit(main())
