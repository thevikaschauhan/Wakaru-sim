import importlib.util
import json
import os
from pathlib import Path
import pickle
import shutil
import socket
import subprocess
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import zlib

import pytest
import redis
from rq import Queue

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('legacy_inventory', ROOT/'ops/runtime/inventory_legacy.py')
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)
FUNCTION = 'app.services.cart_recovery_jobs.run_analysis_job'


@pytest.fixture
def database(tmp_path):
    url = os.environ.get('LEGACY_INVENTORY_TEST_REDIS_URL')
    process = None
    if url:
        parsed = urlsplit(url)
        assert parsed.hostname == '127.0.0.1' and parsed.path == '/15', 'isolated loopback test DB 15 required'
    else:
        binary = shutil.which('redis-server')
        assert binary, 'redis-server or explicit fixture URL required; never skip this proof'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([binary, '--bind', '127.0.0.1', '--port', str(port),
                                    '--save', '', '--appendonly', 'no', '--dir', str(tmp_path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f'redis://127.0.0.1:{port}/15'
    conn = redis.Redis.from_url(url)
    owns_database = False
    try:
        for _ in range(100):
            try:
                conn.ping()
                break
            except redis.ConnectionError:
                time.sleep(.02)
        assert conn.dbsize() == 0, 'fixture database must start empty'
        owns_database = True
        yield conn, url
    finally:
        if process:
            process.terminate()
            process.wait(timeout=5)
        elif owns_database:
            # An exclusive, initially-empty fixture DB; never flush any database.
            keys = list(conn.scan_iter())
            if keys:
                conn.delete(*keys)
        conn.close()


def dump(conn):
    return {k: conn.dump(k) for k in conn.scan_iter()}


def test_fixture_refuses_occupied_database_without_deleting_it(database, monkeypatch, tmp_path):
    conn, url = database
    conn.set('preexisting-record', 'must-survive')
    monkeypatch.setenv('LEGACY_INVENTORY_TEST_REDIS_URL', url)
    attempt = globals()['database'].__wrapped__(tmp_path)
    with pytest.raises(AssertionError, match='must start empty'):
        next(attempt)
    assert conn.get('preexisting-record') == b'must-survive'


def test_real_rq_inventory_preserves_all_records_and_expired_registries(database):
    conn, url = database
    queue = Queue('analyze', connection=conn)
    queue.enqueue(FUNCTION, {'analysis_mode': 'direct_v1', 'email': 'private@example.test'}, job_id='direct',
                  meta={'merchant_id': '10000000-0000-4000-8000-000000000001'})
    queue.enqueue(FUNCTION, {'analysis_mode': 'legacy_simulation'}, job_id='legacy')
    queue.enqueue(FUNCTION, {}, job_id='old')
    for index, (prefix, state) in enumerate(inventory.REGISTRIES.items()):
        job_id = f'retained-{index}'
        job = queue.enqueue(FUNCTION, {}, job_id=job_id)
        conn.lrem(queue.key, 0, job.id)
        conn.hset(job.key, 'status', state)
        conn.zadd(prefix+b'analyze', {job_id: 1})  # Expired entry must NOT be cleaned.
    queue.enqueue('historic.sweeper.repeat', job_id='maintenance')
    conn.lrem(queue.key, 0, 'maintenance')
    conn.zadd('rq:scheduled:maintenance', {'maintenance': 9999999999})
    conn.zadd('rq:failed:analyze', {'missing-hash': 1})
    conn.hset('zep:graph:mirofish_0123456789abcdef', mapping={'graph_kind': 'scratch', 'created_at': '1'})
    conn.zadd('zep:scratch:active', {'mirofish_0123456789abcdef': 1, 'mirofish_fedcba9876543210': 1})
    conn.sadd('zep:merchant:private-merchant:graphs', 'mirofish_0123456789abcdef')
    conn.set('idem:jobs:private-customer', '__pending__')
    conn.set('zep:sweep:next', 'private-next-job')
    conn.set('zep:sweep:lock', 'private-lock-token')
    conn.set('unrecognized:private-id', 'private-data')
    before = dump(conn)
    report = inventory.audit_redis(conn)
    assert report['complete'] and report['stable_two_passes']
    result = report['second']
    assert result['jobs']['modes'] == {'direct_v1': 1, 'legacy_simulation': 1, 'legacy_missing_mode': 7, 'other_callable': 1}
    assert result['jobs']['missing_hash_references'] == 1
    assert result['graph_ledger']['active_without_record'] == 1
    assert result['idempotency']['pending'] == 1
    assert result['legacy_sweep_keys'] == {'lock': 1, 'next': 1}
    assert result['unclassified_keys'] == 1
    for state in inventory.REGISTRIES.values():
        assert result['memberships'][state] >= 1
    assert dump(conn) == before
    raw = json.dumps(report)
    for forbidden in ('private@example.test', 'private-merchant', 'private-customer', '0123456789abcdef',
                      'private-next-job', 'private-lock-token', 'private-id', 'private-data'):
        assert forbidden not in raw
    # Exercise the actual standalone CLI, without importing a configured app.
    run = subprocess.run([os.sys.executable, str(ROOT/'ops/runtime/inventory_legacy.py'), '--redis'],
                         env={**os.environ, 'REDIS_URL': url}, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)['redis']['complete']
    assert dump(conn) == before


def test_pickle_reducers_and_decompression_bombs_never_execute(tmp_path):
    marker = tmp_path/'must-not-exist'
    class Attack:
        def __reduce__(self):
            return (os.system, (f'touch {marker}',))
    for payload in (pickle.dumps(Attack()), zlib.compress(pickle.dumps(Attack())),
                    zlib.compress(b'x'*(inventory.MAX_BYTES+1)), b'corrupt'):
        assert inventory.job_mode(payload) == 'unknown'
    assert not marker.exists()
    for cart, expected in (({}, 'legacy_missing_mode'), ({'analysis_mode': None}, 'unknown'),
                           ({'analysis_mode': 'future'}, 'unknown')):
        assert inventory.job_mode(zlib.compress(pickle.dumps((FUNCTION, None, (cart,), {})))) == expected


def test_raw_reader_refuses_mutations_and_incomplete_scan(database):
    conn, _ = database
    with pytest.raises(PermissionError):
        inventory.Reader(conn).read('DEL', 'anything')
    conn.set('one', '1')
    conn.set('two', '2')
    report = inventory.audit_redis(conn, limit=1)
    assert not report['complete']
    assert report['second']['errors'] == ['key_limit_exceeded']
    assert not report['simulation_removal_authorized']


def test_changing_scan_is_not_accepted_as_complete(database, monkeypatch):
    conn, _ = database
    original = inventory.snapshot
    count = 0
    def changing(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            Queue('analyze', connection=conn).enqueue(FUNCTION, {}, job_id='raced')
        return original(*args, **kwargs)
    monkeypatch.setattr(inventory, 'snapshot', changing)
    report = inventory.audit_redis(conn)
    assert not report['stable_two_passes'] and not report['complete']


def test_file_inventory_only_reads_metadata_and_never_follows_links(tmp_path):
    uploads = tmp_path/'uploads'
    for category in ('projects', 'simulations', 'reports'):
        p = uploads/category/'private-id';p.mkdir(parents=True)
        (p/'private-name.json').write_text('secret customer content')
    before = {str(p):p.read_bytes() for p in uploads.rglob('*') if p.is_file()}
    report = inventory.audit_files(uploads)
    assert report['complete'] and report['files'] == {'projects': 1, 'simulations': 1, 'reports': 1}
    assert 'private' not in json.dumps(report) and 'secret' not in json.dumps(report)
    assert {str(p):p.read_bytes() for p in uploads.rglob('*') if p.is_file()} == before
    (uploads/'escape').symlink_to(tmp_path, target_is_directory=True)
    assert not inventory.audit_files(uploads)['complete']
    assert not inventory.audit_files(tmp_path/'absent')['complete']
    assert not inventory.audit_files(uploads, limit=1)['complete']


def test_provider_listing_is_bounded_deduplicated_and_metadata_only():
    calls = []
    def listing(**kwargs):
        calls.append(kwargs)
        ids = ['mirofish_0123456789abcdef', 'merchant_private', 'other'] if kwargs['page_number'] < 3 else []
        return SimpleNamespace(graphs=[SimpleNamespace(graph_id=i) for i in ids])
    client = SimpleNamespace(graph=SimpleNamespace(list_all=listing))
    report = inventory.audit_zep(client)
    assert report['complete'] and len(calls) == 3
    assert report['counts'] == {'scratch': 1, 'merchant_store': 1, 'other': 1, 'duplicate_list_entries': 3}
    assert 'private' not in json.dumps(report)
    assert not inventory.audit_zep(client, limit=1)['complete']
    assert not report['deletion_receipt']
