"""Restricted operator recovery, never mounted on a service or auto-run.

A CURRENT checkpoint digest must come from an independent trusted control plane.
A digest stored beside a data backup does not establish freshness/authenticity.
"""
from contextlib import contextmanager
import json
from pathlib import Path

import jsonschema
import psycopg
from psycopg.types.json import Jsonb

from .store import KnowledgeStore, StoreConflict, digest

TABLES = {
    'delivery_receipts': ('scope_key,command_key,request_sha256,scope_sha256,operation', 'scope_key,command_key'),
    'cleanup_receipts': ('scope_key,command_key,request_sha256,scope_sha256,status,result', 'scope_key,command_key'),
    'events': ('event_id,tenant_key,scope_key,data_version,kind', 'event_id'),
}
SCHEMA = json.loads((Path(__file__).resolve().parents[2]/'contracts/knowledge/recovery-v1/checkpoint.schema.json').read_text())


class _RecoveryTransaction(KnowledgeStore):
    def __init__(self, store, db):
        super().__init__(store.dsn, store.fence_key)
        self.db = db

    @contextmanager
    def transaction(self, **kwargs):
        yield self.db

    def restore_transaction(self):
        return self.transaction()


def _checkpoint(store, db):
    out = {'version': 'knowledge_checkpoint_v1',
           'fences': _RecoveryTransaction(store, db).retained_checkpoint()}
    for name, (columns, order) in TABLES.items():
        out[name] = [list(row) for row in db.execute(
            'SELECT '+columns+' FROM knowledge_retained.'+name+' ORDER BY '+order+' LIMIT 100001')]
    _validate(store, out, digest(out))
    return out


def _validate(store, checkpoint, expected_digest):
    try:
        jsonschema.Draft202012Validator(SCHEMA).validate(checkpoint)
    except jsonschema.ValidationError as exc:
        raise StoreConflict('checkpoint_schema') from exc
    if digest(checkpoint) != expected_digest or checkpoint['fences']['key_fingerprint'] != store.fingerprint:
        raise StoreConflict('checkpoint_binding')
    subjects = {r[0]: r[1] for r in checkpoint['fences']['subjects']}
    tenants = {r[0] for r in checkpoint['fences']['tenants']}
    for name in ('delivery_receipts', 'cleanup_receipts'):
        rows = checkpoint[name]
        if len({tuple(r[:2]) for r in rows}) != len(rows) or any(r[0] not in subjects for r in rows):
            raise StoreConflict('checkpoint_references')
    events = checkpoint['events']
    if len({r[0] for r in events}) != len(events) or any(
            tenant not in tenants or (key is not None and subjects.get(key) != tenant)
            for _, tenant, key, _, _ in events):
        raise StoreConflict('checkpoint_references')
    for row in checkpoint['cleanup_receipts']:
        if row[4] == 'refused' and (row[5]['removed_sources'] or row[5]['removed_objects']):
            raise StoreConflict('checkpoint_result')


def retained_checkpoint(store):
    """Fences, delivery/cleanup receipts and invalidations from one exclusive snapshot."""
    with store.transaction(quarantined=True, exclusive=True) as db:
        return _checkpoint(store, db)


def restore_checkpoint(store, checkpoint, *, expected_digest):
    """Atomic overlay into quarantine; conflict/omission rolls back every family."""
    _validate(store, checkpoint, expected_digest)
    with psycopg.connect(store.dsn, connect_timeout=2) as db:
        db.execute("SET LOCAL lock_timeout='3s'")
        db.execute("SET LOCAL statement_timeout='10s'")
        # restore_fences verifies quarantine/key and takes the control-row lock.
        txstore = _RecoveryTransaction(store, db)
        txstore.restore_fences(checkpoint['fences'], expected_digest=digest(checkpoint['fences']))
        for name, (columns, _) in TABLES.items():
            count = len(columns.split(','))
            for row in checkpoint[name]:
                values = list(row)
                if name == 'cleanup_receipts':
                    values[-1] = Jsonb(values[-1])
                override = ' OVERRIDING SYSTEM VALUE' if name == 'events' else ''
                db.execute('INSERT INTO knowledge_retained.'+name+'('+columns+')'+override+
                           ' VALUES('+','.join(['%s']*count)+') ON CONFLICT DO NOTHING', values)
        # A conflicting immutable row or omitted pre-existing row must never be
        # silently accepted, including newer receipt history with older fences.
        if _checkpoint(store, db) != checkpoint:
            raise StoreConflict('checkpoint_conflict_or_regression')
        # ALTER SEQUENCE is transactional; failed restores cannot consume/reset it.
        next_id = max((r[0] for r in checkpoint['events']), default=0)+1
        db.execute('ALTER SEQUENCE knowledge_retained.events_event_id_seq RESTART WITH '+str(next_id))


def release_checkpoint(store, *, expected_digest):
    """Verify exact independently trusted retained history AND active data before release."""
    with store.transaction(quarantined=True, exclusive=True) as db:
        if not db.execute('SELECT quarantined FROM knowledge_retained.control WHERE singleton').fetchone()[0]:
            raise StoreConflict('restore_requires_quarantine')
        checkpoint = _checkpoint(store, db)
        if digest(checkpoint) != expected_digest:
            raise StoreConflict('checkpoint_binding')
        _RecoveryTransaction(store, db).release_quarantine(expected_checkpoint_digest=expected_digest)
