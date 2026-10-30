"""Explicit internal maintenance/export helpers. No HTTP or worker registration.

Callers must authorize the exact Engine scope before invoking these functions.
They do not issue aggregate privacy acknowledgments or alter delivery receipts.
"""
import hmac
import json
import re

from psycopg.types.json import Jsonb

from .knowledge import read_knowledge
from .store import StoreConflict


def purge_withdrawn(store, scope):
    """Physically remove withdrawn receipts and dependent assertion histories.

    An assertion's revisions are an indivisible chain in knowledge_batch_v1.
    Removing one revision therefore removes that chain, then its dependents.
    Unrelated assertions, catalog bindings and command hashes remain intact.
    This is a logical SQL row purge, not backup/WAL/media sanitization.
    """
    with store.transaction() as db:
        tenant, key, payload, version = store.bound(db, scope)
        records = [] if payload is None else payload['objects']
        candidates = list({store.key('source', [s['source_id'], s['revision']]) for r in records for s in r['sources']})
        fences = {r[0] for r in db.execute('SELECT source_key FROM knowledge_retained.withdrawals WHERE scope_key=%s AND source_key=ANY(%s)', (key, candidates))}
        removed = {r['object_id'] for r in records if any(
            store.key('source', [s['source_id'], s['revision']]) in fences for s in r['sources'])}
        while True:
            assertions = {r['assertion_id'] for r in records if r['object_id'] in removed}
            expanded = removed | {r['object_id'] for r in records if r['assertion_id'] in assertions
                                  or (r['derivation'] is not None and removed.intersection(r['derivation']['input_ids']))}
            if expanded == removed:
                break
            removed = expanded
        remaining = [r for r in records if r['object_id'] not in removed]
        cleaned = None if not remaining else {
            **payload, 'objects': remaining,
            'links': [e for e in payload['links'] if e['from'] not in removed and e['to'] not in removed],
        }
        deleted = db.execute('DELETE FROM knowledge_data.sources s USING knowledge_retained.withdrawals w '
                             'WHERE s.scope_key=%s AND w.scope_key=s.scope_key AND w.source_key=s.source_key '
                             'RETURNING s.source_key', (key,)).fetchall()
        if cleaned is not None:
            sources = [r[0] for r in db.execute('SELECT receipt FROM knowledge_data.sources WHERE scope_key=%s', (key,))]
            entities = [r[0] for r in db.execute('SELECT receipt FROM knowledge_data.catalog WHERE scope_key=%s', (key,))]
            read_knowledge(json.dumps(cleaned), scope=scope, sources=sources, entities=entities,
                           at=db.execute('SELECT clock_timestamp()').fetchone()[0])
        if deleted or removed:
            for record in records:
                if record['object_id'] in removed:
                    for namespace, value in [('purged-object', record['object_id']), ('purged-assertion', record['assertion_id'])]:
                        db.execute('INSERT INTO knowledge_retained.withdrawals VALUES(%s,%s) ON CONFLICT DO NOTHING',
                                   (key, store.key(namespace, value)))
            version += 1
            db.execute('UPDATE knowledge_data.subjects SET payload=%s,data_version=%s WHERE scope_key=%s',
                       (None if cleaned is None else Jsonb(cleaned), version, key))
            db.execute('UPDATE knowledge_retained.subjects SET data_version=%s WHERE scope_key=%s', (version, key))
            # A payload change invalidates consumers just like another write.
            store.event(db, tenant, key, version, 'write')
        return {'version': version, 'removed_sources': len(deleted), 'removed_objects': len(removed),
                'whole_application_complete': False}


# Constant-only SQL identifiers. Never interpolate caller-supplied family names.
_FAMILIES = {
    'withdrawals': ('knowledge_retained.withdrawals', 'source_key', 'source_key'),
    'delivery_receipts': ('knowledge_retained.delivery_receipts', 'command_key',
                          'command_key,request_sha256,scope_sha256,operation'),
    'events': ('knowledge_retained.events', 'event_id', 'event_id,data_version,kind'),
}


def export_retained_page(store, scope, family, *, limit=100, cursor=None, snapshot=None):
    """Bounded, scope-authorized metadata export, including after erasure.

    Pass the returned snapshot to every family/page of an export. Any ledger
    change forces a restart. Active payload export is a separate operation;
    these pages are not a whole-application export or a restore checkpoint.
    Migration 002 is required; its absence fails instead of omitting receipts.
    """
    if not isinstance(family, str) or family not in _FAMILIES or type(limit) is not int or not 1 <= limit <= 100:
        raise StoreConflict('export_page_binding')
    tenant, key = store.keys(scope)
    with store.transaction(exclusive=True) as db:
        merchant = db.execute('SELECT erased FROM knowledge_retained.tenants WHERE tenant_key=%s', (tenant,)).fetchone()
        fence = db.execute('SELECT tenant_key,identity_revision,privacy_generation,data_version,erased '
                           'FROM knowledge_retained.subjects WHERE scope_key=%s', (key,)).fetchone()
        if (not merchant or not fence or fence[0] != tenant
                or fence[1:3] != (scope['identity_revision'], scope['privacy_generation'])):
            raise StoreConflict('subject_fenced')
        counts = {}
        for name, (table, _, _) in _FAMILIES.items():
            condition = '(scope_key=%s OR (scope_key IS NULL AND tenant_key=%s))' if name == 'events' else 'scope_key=%s'
            params = (key, tenant) if name == 'events' else (key,)
            counts[name] = db.execute('SELECT count(*) FROM ' + table + ' WHERE ' + condition, params).fetchone()[0]
        token = store.key('retained-export-snapshot', [scope, list(merchant), list(fence), counts])
        if snapshot is not None and snapshot != token:
            raise StoreConflict('export_changed')
        after = 0 if family == 'events' else ''
        if cursor is not None:
            if not isinstance(cursor, dict) or set(cursor) != {'after', 'snapshot', 'mac'}:
                raise StoreConflict('export_cursor')
            after = cursor['after']
            valid = type(after) is int and 0 < after < 2**63 if family == 'events' else isinstance(after, str) and re.fullmatch('[0-9a-f]{64}', after)
            if not valid or any(not isinstance(cursor[k], str) or not re.fullmatch('[0-9a-f]{64}', cursor[k]) for k in ('mac', 'snapshot')):
                raise StoreConflict('export_cursor')
            expected = store.key('retained-export-cursor', [scope, family, after, cursor['snapshot']])
            if not hmac.compare_digest(cursor['mac'], expected):
                raise StoreConflict('export_cursor')
            if cursor['snapshot'] != token:
                raise StoreConflict('export_changed')
        table, column, fields = _FAMILIES[family]
        condition = '(scope_key=%s OR (scope_key IS NULL AND tenant_key=%s))' if family == 'events' else 'scope_key=%s'
        params = (key, tenant) if family == 'events' else (key,)
        rows = db.execute('SELECT ' + fields + ' FROM ' + table + ' WHERE ' + condition
                          + ' AND ' + column + '>%s ORDER BY ' + column + ' LIMIT %s', (*params, after, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if more:
            after = rows[-1][0]
            next_cursor = {'after': after, 'snapshot': token,
                           'mac': store.key('retained-export-cursor', [scope, family, after, token])}
        return {'version': 'knowledge_retained_export_v1', 'scope': scope, 'family': family,
                'fields': fields.split(','), 'rows': [list(r) for r in rows], 'counts': counts,
                'subject': {'identity_revision': fence[1], 'privacy_generation': fence[2],
                            'data_version': fence[3], 'erased': fence[4]},
                'merchant_erased': merchant[0], 'snapshot': token, 'next_cursor': next_cursor,
                'whole_application_complete': False}
