"""Read-only legacy inventory. Never imports the app, executes jobs or runs cleanup.

An empty live scan is not a drain fence, an erasure receipt or deletion approval.
Only aggregate metadata leaves this module. Credentials come from environment.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import pickle
import pickletools
import re
import stat
import time
import zlib

READ_COMMANDS = frozenset({'SCAN', 'TYPE', 'LRANGE', 'ZRANGE', 'SSCAN', 'HGET',
                           'HMGET', 'HSTRLEN', 'STRLEN', 'GET', 'INFO'})
REGISTRIES = {b'rq:wip:': 'started', b'rq:finished:': 'finished',
              b'rq:failed:': 'failed', b'rq:deferred:': 'deferred',
              b'rq:scheduled:': 'scheduled', b'rq:canceled:': 'canceled'}
JOB_FUNCTIONS = {'app.services.cart_recovery_jobs.run_analysis_job',
                 'backend.app.services.cart_recovery_jobs.run_analysis_job'}
STATUSES = {'queued', 'started', 'finished', 'failed', 'deferred', 'scheduled', 'canceled', 'stopped'}
SCRATCH = re.compile(r'mirofish_[0-9a-f]{16}\Z')
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
MAX_BYTES = 1024 * 1024


class Incomplete(Exception):
    pass


class PrimitiveUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise ValueError('objects_not_allowed')

    def persistent_load(self, pid):
        raise ValueError('persistent_objects_not_allowed')


def primitive_pickle(raw, compressed=False):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('payload_size')
    if compressed:
        stream = zlib.decompressobj()
        raw = stream.decompress(raw, MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or not stream.eof or stream.unused_data:
            raise ValueError('compressed_payload_size_or_framing')
    denied = {'GLOBAL', 'STACK_GLOBAL', 'REDUCE', 'BUILD', 'INST', 'OBJ',
              'NEWOBJ', 'NEWOBJ_EX', 'EXT1', 'EXT2', 'EXT4', 'PERSID', 'BINPERSID'}
    count = 0
    for opcode, arg, _ in pickletools.genops(raw):
        count += 1
        if opcode.name in denied or count > 100000:
            raise ValueError('unsafe_pickle')
        if opcode.name in {'PUT', 'BINPUT', 'LONG_BINPUT'} and int(arg) > 100000:
            raise ValueError('pickle_memo_limit')
    stream = io.BytesIO(raw)
    value = PrimitiveUnpickler(stream).load()
    if stream.read(1):
        raise ValueError('trailing_pickle')
    return value


def job_mode(raw):
    try:
        # RQ 1.16 stores a zlib-compressed pickle. Old uncompressed records are
        # also decoded, but globals/reducers/custom objects are always refused.
        value = primitive_pickle(raw, compressed=not raw.startswith(b'\x80'))
        if not isinstance(value, tuple) or len(value) != 4:
            return 'unknown'
        function, instance, args, kwargs = value
        if not isinstance(function, str) or function not in JOB_FUNCTIONS:
            return 'other_callable'
        if instance is not None or not isinstance(args, (list, tuple)) or not isinstance(kwargs, dict):
            return 'unknown'
        if len(args) == 1 and not kwargs:
            cart = args[0]
        elif not args and set(kwargs) == {'cart_dict'}:
            cart = kwargs['cart_dict']
        else:
            return 'unknown'
        if not isinstance(cart, dict):
            return 'unknown'
        if 'analysis_mode' not in cart:
            return 'legacy_missing_mode'
        mode = cart['analysis_mode']
        return mode if mode in ('direct_v1', 'legacy_simulation') else 'unknown'
    except Exception:
        return 'unknown'


class Reader:
    def __init__(self, connection, limit=10000, seconds=60):
        self.connection = connection
        self.limit = limit
        self.deadline = time.monotonic() + seconds
        self.commands = 0

    def read(self, command, *args):
        if command not in READ_COMMANDS:
            raise PermissionError('non_read_command')
        self.commands += 1
        if self.commands > self.limit * 20 or time.monotonic() > self.deadline:
            raise Incomplete('read_budget_exceeded')
        return self.connection.execute_command(command, *args)

    def scan(self):
        cursor, found = 0, set()
        while True:
            cursor, batch = self.read('SCAN', cursor, 'COUNT', 200)
            found.update(batch)
            if len(found) > self.limit:
                raise Incomplete('key_limit_exceeded')
            if int(cursor) == 0:
                return sorted(found)

    def members(self, key, kind):
        if kind in (b'list', b'zset'):
            values = self.read('LRANGE' if kind == b'list' else 'ZRANGE', key, 0, self.limit)
        elif kind == b'set':
            cursor, found = 0, set()
            while True:
                cursor, batch = self.read('SSCAN', key, cursor, 'COUNT', 200)
                found.update(batch)
                if len(found) > self.limit:
                    raise Incomplete('member_limit_exceeded')
                if int(cursor) == 0:
                    break
            values = sorted(found)
        else:
            raise Incomplete('unexpected_index_type')
        if len(values) > self.limit:
            raise Incomplete('member_limit_exceeded')
        return values

    def blob(self, key, field):
        if self.read('HSTRLEN', key, field) > MAX_BYTES:
            raise Incomplete('payload_limit_exceeded')
        value = self.read('HGET', key, field)
        if value and len(value) > MAX_BYTES:
            raise Incomplete('payload_limit_exceeded')
        return value


def snapshot(connection, secret, limit=10000, seconds=60):
    reader = Reader(connection, limit, seconds)
    report = {'complete': False, 'errors': [], 'jobs': {}, 'memberships': {},
              'graph_ledger': {}, 'idempotency': {}, 'other_rq_keys': {}}
    digest = hmac.new(secret, digestmod=hashlib.sha256)
    active, graph_records, jobs, refs, merchant_refs = set(), {}, {}, set(), set()
    queues = {}
    def queue_record(name):
        label = name.decode() if name in (b'analyze', b'maintenance') else 'other_' + hmac.new(secret, name, hashlib.sha256).hexdigest()[:12]
        return queues.setdefault(label, {'registered': False, 'memberships': {}})
    memberships, modes, statuses, ownership, ledger, idem, other = (Counter() for _ in range(7))
    sweep_keys, unclassified_keys = Counter(), 0
    try:
        keys = reader.scan()
        for key in keys:
            kind = reader.read('TYPE', key)
            # Heartbeats and result bodies are not read. Their keys are counted.
            if key.startswith(b'rq:job:') and kind == b'hash':
                job_id = key[len(b'rq:job:'):]
                raw = reader.blob(key, 'data')
                mode = job_mode(raw) if raw else 'unknown'
                status, retry = reader.read('HMGET', key, 'status', 'retries_left')
                status = status.decode('utf-8', 'replace') if status else 'unknown'
                status = status if status in STATUSES else 'unknown'
                modes[mode] += 1
                statuses[status] += 1
                jobs[job_id] = mode
                try:
                    if retry is not None and int(retry) > 0:
                        other['jobs_with_retries_left'] += 1
                except ValueError:
                    other['invalid_retry_metadata'] += 1
                meta = reader.blob(key, 'meta')
                try:
                    meta = primitive_pickle(meta) if meta else {}
                    merchant = meta.get('merchant_id') if isinstance(meta, dict) else None
                    owner = ('sentinel' if merchant == '00000000-0000-0000-0000-000000000000'
                             else 'bound' if isinstance(merchant, str) and UUID.fullmatch(merchant)
                             else 'unbound')
                except Exception:
                    owner = 'unreadable'
                ownership[owner] += 1
                digest.update(key + (raw or b'') + status.encode() + (retry or b''))
            elif key.startswith(b'rq:queue:') or any(key.startswith(p) for p in REGISTRIES):
                if key.startswith(b'rq:queue:'):
                    state = 'intermediate' if key.endswith(b':intermediate') else 'queued'
                    name = key[len(b'rq:queue:'):]
                    if state == 'intermediate':
                        name = name[:-len(b':intermediate')]
                    expected = b'list'
                else:
                    prefix = next(prefix for prefix in REGISTRIES if key.startswith(prefix))
                    state, name = REGISTRIES[prefix], key[len(prefix):]
                    expected = b'zset'
                if kind != expected:
                    raise Incomplete('unexpected_registry_type')
                values = reader.members(key, kind)
                memberships[state] += len(values)
                queue_record(name)['memberships'][state] = len(values)
                refs.update(values)
                digest.update(key + repr(values).encode())
            elif key.startswith(b'zep:graph:'):
                if kind != b'hash':
                    raise Incomplete('unexpected_graph_record_type')
                values = reader.read('HMGET', key, 'graph_kind', 'created_at', 'deleted_at', 'merchant_id')
                graph_records[key[len(b'zep:graph:'):]] = values
                ledger['tombstoned' if values[2] else 'live'] += 1
                ledger['unattributed'] += int(not values[3] or values[3] == b'00000000-0000-0000-0000-000000000000')
                try:
                    if int(values[1]) <= 0:
                        raise ValueError()
                except (ValueError, TypeError):
                    ledger['unknown_creation_time'] += 1
                ledger['non_scratch_ids'] += int(not SCRATCH.fullmatch(key[len(b'zep:graph:'):].decode('utf-8', 'replace')))
                digest.update(key + repr(values).encode())
            elif key == b'zep:scratch:active' or (key.startswith(b'zep:merchant:') and key.endswith(b':graphs')):
                expected = b'zset' if key == b'zep:scratch:active' else b'set'
                if kind != expected:
                    raise Incomplete('unexpected_graph_index_type')
                values = reader.members(key, kind)
                if expected == b'zset':
                    active.update(values)
                else:
                    ledger['merchant_sets'] += 1
                    ledger['merchant_memberships'] += len(values)
                    merchant_refs.update(values)
                digest.update(key + repr(values).encode())
            elif key.startswith(b'zep:sweep:'):
                # The historical scheduler also retained a chain marker and
                # lock outside the RQ namespace. Count without exposing values.
                label = {b'zep:sweep:lock': 'lock', b'zep:sweep:next': 'next'}.get(key, 'other')
                sweep_keys[label] += 1
                digest.update(key + kind)
            elif key.startswith(b'idem:'):
                idem['keys'] += 1
                if kind != b'string':
                    raise Incomplete('unexpected_idempotency_type')
                if reader.read('STRLEN', key) == len(b'__pending__') and reader.read('GET', key) == b'__pending__':
                    idem['pending'] += 1
            elif key == b'rq:queues':
                if kind != b'set':
                    raise Incomplete('unexpected_queue_directory_type')
                values = reader.members(key, kind)
                for value in values:
                    if not value.startswith(b'rq:queue:'):
                        raise Incomplete('invalid_registered_queue')
                    queue_record(value[len(b'rq:queue:'):])['registered'] = True
                digest.update(key + repr(values).encode())
            elif key.startswith(b'rq:'):
                category = ('worker' if key.startswith(b'rq:worker') else
                            'scheduler' if b'scheduler' in key else
                            'job_related' if key.startswith(b'rq:job:') else 'other')
                other[category] += 1
            else:
                unclassified_keys += 1
        report['complete'] = True
    except Exception as exc:
        report['errors'].append(str(exc) if isinstance(exc, Incomplete) else type(exc).__name__)
    report.update(jobs={'total': len(jobs), 'modes': dict(modes), 'statuses': dict(statuses),
                        'ownership': dict(ownership), 'unindexed': len(set(jobs) - refs),
                        'missing_hash_references': len(refs - set(jobs))},
                  memberships=dict(memberships), queues=queues, idempotency=dict(idem), other_rq_keys=dict(other),
                  legacy_sweep_keys=dict(sweep_keys), unclassified_keys=unclassified_keys)
    ledger['active_index'] = len(active)
    ledger['active_without_record'] = len(active - set(graph_records))
    ledger['active_tombstoned'] = sum(bool(graph_records[g][2]) for g in active & set(graph_records))
    ledger['live_without_active_index'] = sum(not v[2] and g not in active for g, v in graph_records.items())
    ledger['merchant_members_without_record'] = len(merchant_refs - set(graph_records))
    report['graph_ledger'] = dict(ledger)
    return report, digest.hexdigest(), active


def audit_redis(connection, limit=10000, seconds=60):
    secret = os.urandom(32)
    first, a, _ = snapshot(connection, secret, limit, seconds)
    second, b, _ = snapshot(connection, secret, limit, seconds)
    stable = first == second and a == b
    return {'first': first, 'second': second, 'stable_two_passes': stable,
            'complete': first['complete'] and second['complete'] and stable,
            'quiescence_proven': False, 'simulation_removal_authorized': False}


def audit_files(root, limit=10000):
    root = Path(root)
    counts, sizes = Counter(), Counter()
    report = {'complete': False, 'files': {}, 'bytes': {}, 'symlinks': 0, 'special_files': 0, 'errors': []}
    try:
        if root.is_symlink() or not root.is_dir():
            raise Incomplete('root_missing_or_symlink')
        stack, entries = [(root, ())], 0
        while stack:
            directory, parts = stack.pop()
            with os.scandir(directory) as children:
                for entry in children:
                    entries += 1
                    if entries > limit:
                        raise Incomplete('file_limit_exceeded')
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        report['symlinks'] += 1
                    elif stat.S_ISDIR(info.st_mode):
                        stack.append((Path(entry.path), parts + (entry.name,)))
                    elif stat.S_ISREG(info.st_mode):
                        category = next((p for p in parts if p in ('projects', 'simulations', 'reports')), 'other')
                        counts[category] += 1
                        sizes[category] += info.st_size
                    else:
                        report['special_files'] += 1
        report['complete'] = report['symlinks'] == report['special_files'] == 0
    except Exception as exc:
        report['errors'].append(str(exc) if isinstance(exc, Incomplete) else type(exc).__name__)
    report.update(files=dict(counts), bytes=dict(sizes))
    return report


def audit_zep(client, limit=100, page_size=100):
    counts, seen = Counter(), set()
    report = {'complete': False, 'counts': {}, 'pages': 0, 'errors': [], 'deletion_receipt': False}
    try:
        for page in range(1, limit + 1):
            response = client.graph.list_all(page_number=page, page_size=page_size)
            report['pages'] += 1
            graphs = getattr(response, 'graphs', None)
            if graphs is None:
                raise Incomplete('missing_graph_list')
            if not graphs:
                report['complete'] = True
                break
            for graph in graphs:
                graph_id = getattr(graph, 'graph_id', None)
                if not isinstance(graph_id, str):
                    raise Incomplete('missing_graph_identity')
                if graph_id in seen:
                    counts['duplicate_list_entries'] += 1
                    continue
                seen.add(graph_id)
                category = ('scratch' if SCRATCH.fullmatch(graph_id) else
                            'merchant_store' if graph_id.startswith('merchant_') else 'other')
                counts[category] += 1
        else:
            raise Incomplete('page_limit_exceeded')
    except Exception as exc:
        report['errors'].append(str(exc) if isinstance(exc, Incomplete) else type(exc).__name__)
    report['counts'] = dict(counts)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--redis', action='store_true', help='Read REDIS_URL, never print it')
    parser.add_argument('--uploads', type=Path, help='Explicit uploads root on this host')
    parser.add_argument('--zep', action='store_true', help='List graph metadata using ZEP_API_KEY')
    parser.add_argument('--limit', type=int, default=10000)
    args = parser.parse_args()
    if args.limit < 1 or not (args.redis or args.uploads or args.zep):
        parser.error('positive limit and at least one source required')
    report = {'version': 'legacy_inventory_v1', 'observed_at': datetime.now(timezone.utc).isoformat(),
              'simulation_removal_authorized': False}
    for name in ('redis', 'uploads', 'zep'):
        if not getattr(args, name):
            continue
        try:
            if name == 'redis':
                import redis
                connection = redis.Redis.from_url(os.environ['REDIS_URL'], socket_timeout=5, socket_connect_timeout=5)
                try:
                    report[name] = audit_redis(connection, args.limit)
                finally:
                    connection.close()
            elif name == 'uploads':
                report[name] = audit_files(args.uploads, args.limit)
            else:
                from zep_cloud.client import Zep
                report[name] = audit_zep(Zep(api_key=os.environ['ZEP_API_KEY'], timeout=10))
        except Exception as exc:
            report[name] = {'complete': False, 'errors': [type(exc).__name__]}
    print(json.dumps(report, sort_keys=True))
    return 0 if all(v.get('complete') for v in report.values() if isinstance(v, dict)) else 2


if __name__ == '__main__':
    raise SystemExit(main())
