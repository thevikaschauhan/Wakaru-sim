"""Local, opt-in PostgreSQL knowledge lifecycle. No startup wiring or migration.

All entry points are internal: scope and source approval come from a trusted
Engine adapter. The database role must not be exposed to tenants. Retained HMAC
fences and current source withdrawals must survive data backup restoration.
"""
import hashlib
import hmac
import json
from contextlib import contextmanager
from pathlib import Path

import jsonschema
import psycopg
from psycopg.types.json import Jsonb
import rfc8785

from .knowledge import KnowledgeError, _check, _time, read_knowledge


class StoreConflict(KnowledgeError):
    pass


def digest(value):
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


class KnowledgeStore:
    def __init__(self, dsn, fence_key):
        if not isinstance(fence_key, bytes) or len(fence_key) < 32:
            raise ValueError('fence_key_required')
        self.dsn, self.fence_key = dsn, fence_key
        self.fingerprint = self.key('fingerprint', 'knowledge-v1')

    def key(self, namespace, value):
        return hmac.new(self.fence_key, rfc8785.dumps([namespace, value]), hashlib.sha256).hexdigest()

    def keys(self, scope):
        _check(scope, 'scope')
        if scope['subject_kind'] == 'merchant' and scope['subject_id'] != scope['merchant_id']:
            raise StoreConflict('scope_binding')
        return (self.key('tenant', scope['merchant_id']), self.key('scope',
                [scope[k] for k in ('merchant_id', 'store_id', 'subject_kind', 'subject_id')]))

    @contextmanager
    def transaction(self, *, quarantined=False, exclusive=False):
        with psycopg.connect(self.dsn, connect_timeout=2) as db:
            db.execute("SET LOCAL lock_timeout='3s'")
            db.execute("SET LOCAL statement_timeout='10s'")
            row = db.execute('SELECT quarantined,key_fingerprint FROM knowledge_retained.control WHERE singleton FOR ' + ('UPDATE' if exclusive else 'SHARE')).fetchone()
            if not row or row[1] != self.fingerprint or (row[0] and not quarantined):
                raise StoreConflict('quarantined_or_key_mismatch')
            yield db

    def bootstrap_empty(self):
        """Explicit fixture/operator action, permitted only for a new empty store."""
        with psycopg.connect(self.dsn) as db:
            row = db.execute('SELECT quarantined,key_fingerprint FROM knowledge_retained.control WHERE singleton FOR UPDATE').fetchone()
            if not row or not row[0] or row[1] is not None:
                raise StoreConflict('bootstrap_not_empty')
            for table in ('knowledge_data.subjects', 'knowledge_retained.subjects', 'knowledge_retained.tenants', 'knowledge_retained.withdrawals', 'knowledge_retained.events'):
                if db.execute('SELECT EXISTS(SELECT 1 FROM '+table+')').fetchone()[0]:
                    raise StoreConflict('bootstrap_not_empty')
            db.execute('UPDATE knowledge_retained.control SET quarantined=false,key_fingerprint=%s', (self.fingerprint,))

    def provision(self, scope):
        """Provision only a caller-authenticated Engine identity, never inferred identity."""
        tenant, key = self.keys(scope)
        with self.transaction() as db:
            db.execute('INSERT INTO knowledge_retained.tenants VALUES(%s,false) ON CONFLICT DO NOTHING', (tenant,))
            if db.execute('SELECT erased FROM knowledge_retained.tenants WHERE tenant_key=%s FOR SHARE', (tenant,)).fetchone()[0]:
                raise StoreConflict('merchant_erased')
            db.execute('INSERT INTO knowledge_retained.subjects(scope_key,tenant_key,identity_revision,privacy_generation) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING', (key, tenant, scope['identity_revision'], scope['privacy_generation']))
            fence = db.execute('SELECT identity_revision,privacy_generation,data_version,erased FROM knowledge_retained.subjects WHERE scope_key=%s FOR UPDATE', (key,)).fetchone()
            if fence[:2] != (scope['identity_revision'], scope['privacy_generation']) or fence[3]:
                raise StoreConflict('subject_fenced')
            # Missing rows after a restore are never silently re-created.
            if fence[2] != 0 and not db.execute('SELECT 1 FROM knowledge_data.subjects WHERE scope_key=%s', (key,)).fetchone():
                raise StoreConflict('restore_gap')
            db.execute('INSERT INTO knowledge_data.subjects VALUES(%s,%s,NULL,0) ON CONFLICT DO NOTHING', (key, Jsonb(scope)))
            self.bound(db, scope)

    def bound(self, db, scope):
        tenant, key = self.keys(scope)
        merchant = db.execute('SELECT erased FROM knowledge_retained.tenants WHERE tenant_key=%s FOR SHARE', (tenant,)).fetchone()
        if not merchant or merchant[0]:
            raise StoreConflict('merchant_fenced')
        fence = db.execute('SELECT identity_revision,privacy_generation,data_version,erased,tenant_key FROM knowledge_retained.subjects WHERE scope_key=%s FOR UPDATE', (key,)).fetchone()
        if not fence or fence[3] or fence[4] != tenant or fence[:2] != (scope['identity_revision'], scope['privacy_generation']):
            raise StoreConflict('subject_fenced')
        data = db.execute('SELECT scope,payload,data_version FROM knowledge_data.subjects WHERE scope_key=%s', (key,)).fetchone()
        if not data or data[0] != scope or data[2] != fence[2]:
            raise StoreConflict('restore_gap')
        return tenant, key, data[1], data[2]

    def event(self, db, tenant, key, version, kind):
        db.execute('INSERT INTO knowledge_retained.events(tenant_key,scope_key,data_version,kind) VALUES(%s,%s,%s,%s)', (tenant, key, version, kind))

    def capture_source(self, scope, receipt, content):
        """Internal approved-source adapter boundary; raw content is not retained here."""
        _check(receipt, 'source')
        if receipt['scope'] != scope or not isinstance(content, bytes) or len(content) > 200_000 or hashlib.sha256(content).hexdigest() != receipt['content_sha256']:
            raise StoreConflict('source_content_binding')
        skey = self.key('source', [receipt['source_id'], receipt['revision']])
        with self.transaction() as db:
            _, key, _, _ = self.bound(db, scope)
            if _time(receipt['observed_at']) > db.execute('SELECT clock_timestamp()').fetchone()[0]:
                raise StoreConflict('future_source')
            if db.execute('SELECT 1 FROM knowledge_retained.withdrawals WHERE scope_key=%s AND source_key=%s', (key,skey)).fetchone():
                raise StoreConflict('source_withdrawn')
            existing = db.execute('SELECT receipt FROM knowledge_data.sources WHERE scope_key=%s AND source_key=%s', (key,skey)).fetchone()
            if existing:
                if existing[0] != receipt: raise StoreConflict('source_revision_conflict')
                return
            if db.execute('SELECT count(*) FROM knowledge_data.sources WHERE scope_key=%s', (key,)).fetchone()[0] >= 256:
                raise StoreConflict('source_capacity')
            db.execute('INSERT INTO knowledge_data.sources VALUES(%s,%s,%s)', (key,skey,Jsonb(receipt)))

    def capture_entity(self, scope, receipt):
        _check(receipt, 'catalog_entity')
        if any(receipt[k] != scope[k] for k in ('merchant_id','store_id')):
            raise StoreConflict('entity_binding')
        ekey = self.key('entity', receipt['entity'])
        with self.transaction() as db:
            _, key, _, _ = self.bound(db, scope)
            if db.execute('SELECT count(*) FROM knowledge_data.catalog WHERE scope_key=%s', (key,)).fetchone()[0] >= 256 and not db.execute('SELECT 1 FROM knowledge_data.catalog WHERE scope_key=%s AND entity_key=%s', (key,ekey)).fetchone():
                raise StoreConflict('catalog_capacity')
            db.execute('INSERT INTO knowledge_data.catalog VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (key,ekey,Jsonb(receipt)))

    def save(self, raw, *, scope, command_id, expected_version):
        if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128 or type(expected_version) is not int or expected_version < 0:
            raise StoreConflict('command_binding')
        with self.transaction() as db:
            tenant, key, previous, version = self.bound(db, scope)
            # Historical records keep their original receipts. Withdrawal fences
            # prevent current reads and new assertions, not unrelated future writes.
            sources = [r[0] for r in db.execute('SELECT receipt FROM knowledge_data.sources WHERE scope_key=%s', (key,))]
            withdrawn = {r[0] for r in db.execute('SELECT source_key FROM knowledge_retained.withdrawals WHERE scope_key=%s', (key,))}
            entities = [r[0] for r in db.execute('SELECT receipt FROM knowledge_data.catalog WHERE scope_key=%s', (key,))]
            payload = read_knowledge(raw, scope=scope, sources=sources, entities=entities, at=db.execute('SELECT clock_timestamp()').fetchone()[0])
            self.check_purged_records(db, key, payload)
            historical_ids = set() if previous is None else {r['object_id'] for r in previous['objects']}
            uses_withdrawn = False
            for record in payload['objects']:
                if any(self.key('source',[s['source_id'],s['revision']]) in withdrawn for s in record['sources']):
                    uses_withdrawn = True
                    if record['object_id'] not in historical_ids:
                        raise StoreConflict('source_withdrawn')
            if uses_withdrawn and payload == previous:
                raise StoreConflict('source_withdrawn')
            request_hash = digest([payload,expected_version])
            ckey = self.key('command', command_id)
            command = db.execute('SELECT request_hash,data_version FROM knowledge_data.commands WHERE scope_key=%s AND command_key=%s', (key,ckey)).fetchone()
            if command:
                if command[0] != request_hash: raise StoreConflict('idempotency_conflict')
                return {'version':command[1], 'replayed':True}
            if expected_version != version: raise StoreConflict('stale_version')
            if previous:
                self.transition(previous, payload)
            version += 1
            db.execute('UPDATE knowledge_data.subjects SET payload=%s,data_version=%s WHERE scope_key=%s', (Jsonb(payload),version,key))
            db.execute('UPDATE knowledge_retained.subjects SET data_version=%s WHERE scope_key=%s', (version,key))
            db.execute('INSERT INTO knowledge_data.commands VALUES(%s,%s,%s,%s)', (key,ckey,request_hash,version))
            self.event(db,tenant,key,version,'write')
            return {'version':version,'replayed':False}

    def check_purged_records(self, db, key, payload):
        """Opaque object/assertion tombstones share the retained withdrawal ledger.

        Separate HMAC namespaces prevent collisions with source revision fences.
        Keeping these in the existing checkpoint prevents restore from losing them.
        """
        candidates = [self.key(namespace, record[field]) for record in payload['objects']
                      for namespace, field in [('purged-object', 'object_id'), ('purged-assertion', 'assertion_id')]]
        fences = {r[0] for r in db.execute('SELECT source_key FROM knowledge_retained.withdrawals WHERE scope_key=%s AND source_key=ANY(%s)', (key, candidates))}
        for record in payload['objects']:
            if (self.key('purged-object', record['object_id']) in fences
                    or self.key('purged-assertion', record['assertion_id']) in fences):
                raise StoreConflict('object_purged')

    @staticmethod
    def transition(previous, payload):
        records = {r['object_id']:r for r in payload['objects']}
        for old in previous['objects']:
            new = records.get(old['object_id'])
            if not new or {k:v for k,v in old.items() if k!='status'} != {k:v for k,v in new.items() if k!='status'}:
                raise StoreConflict('immutable_record')
            if old['status'] != 'current' and new['status'] != old['status']:
                raise StoreConflict('terminal_record')
        if not {digest(e) for e in previous['links']} <= {digest(e) for e in payload['links']}:
            raise StoreConflict('immutable_lineage')

    def current(self, scope):
        with self.transaction() as db:
            _, key, payload, version = self.bound(db, scope)
            now = db.execute('SELECT clock_timestamp()').fetchone()[0]
            withdrawn = {r[0] for r in db.execute('SELECT source_key FROM knowledge_retained.withdrawals WHERE scope_key=%s', (key,))}
            records = [] if payload is None else payload['objects']
            visible = {r['object_id']:r for r in records if r['status']=='current' and _time(r['valid_from']) <= now and all(r[k] is None or now < _time(r[k]) for k in ('valid_to','expires_at')) and all(self.key('source',[s['source_id'],s['revision']]) not in withdrawn for s in r['sources'])}
            visible = {key:r for key,r in visible.items() if r['derivation'] is None or all(i in visible for i in r['derivation']['input_ids'])}
            links = [] if payload is None else [e for e in payload['links'] if e['from'] in visible and e['to'] in visible]
            return {'scope':scope,'version':version,'objects':list(visible.values()),'links':links}

    def export_subject(self, scope):
        with self.transaction() as db:
            _, key, payload, version = self.bound(db, scope)
            return {'scope':scope,'version':version,'payload':payload,
                    'sources':[r[0] for r in db.execute('SELECT receipt FROM knowledge_data.sources WHERE scope_key=%s ORDER BY source_key',(key,))],
                    'catalog':[r[0] for r in db.execute('SELECT receipt FROM knowledge_data.catalog WHERE scope_key=%s ORDER BY entity_key',(key,))],
                    'commands':[list(r) for r in db.execute('SELECT command_key,request_hash,data_version FROM knowledge_data.commands WHERE scope_key=%s ORDER BY command_key',(key,))]}

    def withdraw_source(self, scope, source_ref):
        _check(source_ref,'source_ref')
        skey = self.key('source',[source_ref['source_id'],source_ref['revision']])
        with self.transaction() as db:
            tenant,key,_,version = self.bound(db,scope)
            inserted = db.execute('INSERT INTO knowledge_retained.withdrawals VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING source_key',(key,skey)).fetchone()
            if not inserted: return version
            version += 1
            db.execute('UPDATE knowledge_retained.subjects SET data_version=%s WHERE scope_key=%s',(version,key))
            db.execute('UPDATE knowledge_data.subjects SET data_version=%s WHERE scope_key=%s',(version,key))
            self.event(db,tenant,key,version,'source_withdrawal')
            return version

    def revise_identity(self, old_scope, new_scope):
        if self.keys(old_scope) != self.keys(new_scope) or new_scope['identity_revision'] <= old_scope['identity_revision'] or new_scope['privacy_generation'] <= old_scope['privacy_generation']:
            raise StoreConflict('identity_revision_binding')
        with self.transaction() as db:
            tenant,key,_,version = self.bound(db,old_scope)
            db.execute('DELETE FROM knowledge_data.subjects WHERE scope_key=%s',(key,))
            version += 1
            db.execute('UPDATE knowledge_retained.subjects SET identity_revision=%s,privacy_generation=%s,data_version=%s WHERE scope_key=%s',(new_scope['identity_revision'],new_scope['privacy_generation'],version,key))
            db.execute('INSERT INTO knowledge_data.subjects VALUES(%s,%s,NULL,%s)',(key,Jsonb(new_scope),version))
            self.event(db,tenant,key,version,'identity_revision')

    def erase_subject(self, scope):
        tenant,key = self.keys(scope)
        with self.transaction() as db:
            # Allow an idempotent repeat, but never let a stale identity erase a new one.
            db.execute('INSERT INTO knowledge_retained.tenants VALUES(%s,false) ON CONFLICT DO NOTHING',(tenant,))
            merchant = db.execute('SELECT erased FROM knowledge_retained.tenants WHERE tenant_key=%s FOR SHARE',(tenant,)).fetchone()
            db.execute('INSERT INTO knowledge_retained.subjects(scope_key,tenant_key,identity_revision,privacy_generation) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(key,tenant,scope['identity_revision'],scope['privacy_generation']))
            row = db.execute('SELECT identity_revision,privacy_generation,erased,data_version FROM knowledge_retained.subjects WHERE scope_key=%s FOR UPDATE',(key,)).fetchone()
            if not merchant or not row or row[:2] != (scope['identity_revision'],scope['privacy_generation']):
                raise StoreConflict('subject_fenced')
            if not row[2]:
                db.execute('UPDATE knowledge_retained.subjects SET erased=true,data_version=data_version+1 WHERE scope_key=%s',(key,))
                self.event(db,tenant,key,row[3]+1,'subject_erasure')
            db.execute('DELETE FROM knowledge_data.subjects WHERE scope_key=%s',(key,))

    def erase_merchant(self, merchant_id):
        if not isinstance(merchant_id,str) or not merchant_id: raise StoreConflict('merchant_binding')
        tenant = self.key('tenant',merchant_id)
        with self.transaction() as db:
            db.execute('INSERT INTO knowledge_retained.tenants VALUES(%s,true) ON CONFLICT DO NOTHING',(tenant,))
            row = db.execute('SELECT erased FROM knowledge_retained.tenants WHERE tenant_key=%s FOR UPDATE',(tenant,)).fetchone()
            db.execute('UPDATE knowledge_retained.tenants SET erased=true WHERE tenant_key=%s',(tenant,))
            db.execute('UPDATE knowledge_retained.subjects SET erased=true,data_version=data_version+1 WHERE tenant_key=%s AND NOT erased',(tenant,))
            db.execute('DELETE FROM knowledge_data.subjects WHERE scope_key IN (SELECT scope_key FROM knowledge_retained.subjects WHERE tenant_key=%s)',(tenant,))
            if not row[0]: self.event(db,tenant,None,None,'merchant_erasure')

    def quarantine(self):
        with self.transaction(quarantined=True,exclusive=True) as db:
            db.execute('UPDATE knowledge_retained.control SET quarantined=true')

    def retained_checkpoint(self):
        """Restricted operator receipt, obtained from current authoritative ledger."""
        with self.transaction(quarantined=True,exclusive=True) as db:
            return {'version':'knowledge_fences_v1','key_fingerprint':self.fingerprint,
                    'tenants':[list(r) for r in db.execute('SELECT tenant_key,erased FROM knowledge_retained.tenants ORDER BY tenant_key')],
                    'subjects':[list(r) for r in db.execute('SELECT scope_key,tenant_key,identity_revision,privacy_generation,data_version,erased FROM knowledge_retained.subjects ORDER BY scope_key')],
                    'withdrawals':[list(r) for r in db.execute('SELECT scope_key,source_key FROM knowledge_retained.withdrawals ORDER BY scope_key,source_key')]}

    def restore_fences(self, checkpoint, *, expected_digest):
        """Overlay a separately trusted CURRENT checkpoint; never one from the data backup.

        expected_digest must be supplied by the trusted restore control plane.
        It is not an authenticity check when copied from an untrusted backup.
        A restored database remains quarantined until release_quarantine succeeds.
        """
        schema = json.loads((Path(__file__).resolve().parents[2]/'contracts/knowledge/lifecycle-v1/fences.schema.json').read_text())
        try:
            jsonschema.Draft202012Validator(schema).validate(checkpoint)
        except jsonschema.ValidationError as exc:
            raise StoreConflict('checkpoint_schema') from exc
        tenants = {row[0] for row in checkpoint['tenants']}
        subjects = {row[0] for row in checkpoint['subjects']}
        if (len(tenants)!=len(checkpoint['tenants']) or len(subjects)!=len(checkpoint['subjects'])
                or any(row[1] not in tenants for row in checkpoint['subjects'])
                or any(row[0] not in subjects for row in checkpoint['withdrawals'])):
            raise StoreConflict('checkpoint_references')
        if checkpoint['key_fingerprint']!=self.fingerprint or digest(checkpoint)!=expected_digest:
            raise StoreConflict('checkpoint_binding')
        with psycopg.connect(self.dsn) as db:
            control = db.execute('SELECT quarantined,key_fingerprint FROM knowledge_retained.control WHERE singleton FOR UPDATE').fetchone()
            if not control or not control[0] or control[1] not in (None,self.fingerprint): raise StoreConflict('restore_requires_quarantine')
            for key,erased in checkpoint['tenants']:
                db.execute('INSERT INTO knowledge_retained.tenants VALUES(%s,%s) ON CONFLICT(tenant_key) DO UPDATE SET erased=knowledge_retained.tenants.erased OR excluded.erased',(key,erased))
            for key,tenant,identity,generation,version,erased in checkpoint['subjects']:
                old = db.execute('SELECT tenant_key,identity_revision,privacy_generation,data_version,erased FROM knowledge_retained.subjects WHERE scope_key=%s',(key,)).fetchone()
                if old and (old[0]!=tenant or identity<old[1] or generation<old[2] or version<old[3] or (old[4] and not erased)):
                    raise StoreConflict('checkpoint_regression')
                db.execute('INSERT INTO knowledge_retained.subjects VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(scope_key) DO UPDATE SET identity_revision=excluded.identity_revision,privacy_generation=excluded.privacy_generation,data_version=excluded.data_version,erased=excluded.erased',(key,tenant,identity,generation,version,erased))
            for key,source in checkpoint['withdrawals']:
                db.execute('INSERT INTO knowledge_retained.withdrawals VALUES(%s,%s) ON CONFLICT DO NOTHING',(key,source))
            db.execute('UPDATE knowledge_retained.control SET key_fingerprint=%s',(self.fingerprint,))

    def release_quarantine(self):
        """Explicit restore gate. Purge erased fixture data; refuse missing/stale active rows."""
        with self.transaction(quarantined=True,exclusive=True) as db:
            db.execute('DELETE FROM knowledge_data.subjects WHERE scope_key IN (SELECT s.scope_key FROM knowledge_retained.subjects s JOIN knowledge_retained.tenants t USING(tenant_key) WHERE s.erased OR t.erased)')
            rows = db.execute('SELECT s.scope_key,s.tenant_key,s.identity_revision,s.privacy_generation,s.data_version,d.scope,d.data_version FROM knowledge_retained.subjects s JOIN knowledge_retained.tenants t USING(tenant_key) LEFT JOIN knowledge_data.subjects d USING(scope_key) WHERE NOT s.erased AND NOT t.erased').fetchall()
            for key,tenant,identity,generation,version,scope,data_version in rows:
                if not scope or self.keys(scope)!=(tenant,key) or scope['identity_revision']!=identity or scope['privacy_generation']!=generation or data_version!=version:
                    raise StoreConflict('restore_gap')
                payload = db.execute('SELECT payload FROM knowledge_data.subjects WHERE scope_key=%s',(key,)).fetchone()[0]
                if payload is not None:
                    sources = [r[0] for r in db.execute('SELECT receipt FROM knowledge_data.sources WHERE scope_key=%s',(key,))]
                    entities = [r[0] for r in db.execute('SELECT receipt FROM knowledge_data.catalog WHERE scope_key=%s',(key,))]
                    read_knowledge(json.dumps(payload),scope=scope,sources=sources,entities=entities,at=db.execute('SELECT clock_timestamp()').fetchone()[0])
                    self.check_purged_records(db, key, payload)
            if db.execute('SELECT 1 FROM knowledge_data.subjects d LEFT JOIN knowledge_retained.subjects s USING(scope_key) WHERE s.scope_key IS NULL LIMIT 1').fetchone():
                raise StoreConflict('unfenced_restore_data')
            db.execute('UPDATE knowledge_retained.control SET quarantined=false')

    def export_merchant(self, merchant_id):
        """Internal authorized merchant-wide export, serialized against erasure/writes."""
        tenant = self.key('tenant',merchant_id)
        with self.transaction() as db:
            row = db.execute('SELECT erased FROM knowledge_retained.tenants WHERE tenant_key=%s FOR UPDATE',(tenant,)).fetchone()
            if not row or row[0]: raise StoreConflict('merchant_fenced')
            result=[]
            for key,scope in db.execute('SELECT d.scope_key,d.scope FROM knowledge_data.subjects d JOIN knowledge_retained.subjects s USING(scope_key) WHERE s.tenant_key=%s ORDER BY d.scope_key',(tenant,)).fetchall():
                _,_,payload,version=self.bound(db,scope)
                result.append({'scope':scope,'version':version,'payload':payload,
                               'sources':[r[0] for r in db.execute('SELECT receipt FROM knowledge_data.sources WHERE scope_key=%s ORDER BY source_key',(key,))],
                               'catalog':[r[0] for r in db.execute('SELECT receipt FROM knowledge_data.catalog WHERE scope_key=%s ORDER BY entity_key',(key,))],
                               'commands':[list(r) for r in db.execute('SELECT command_key,request_hash,data_version FROM knowledge_data.commands WHERE scope_key=%s ORDER BY command_key',(key,))]})
            return result
