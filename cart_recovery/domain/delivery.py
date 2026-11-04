"""Opt-in command receipts. A historical application receipt is not current authority."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
from pathlib import Path

from flask import Flask, request
import jsonschema
import psycopg
from psycopg.types.json import Jsonb

from cart_recovery.integration_service_auth import ServiceGrants
from .knowledge import FORMAT, KnowledgeError, _pairs, _time
from .service import VALIDATOR as SERVICE_VALIDATOR, execute, response
from .store import KnowledgeStore, StoreConflict, digest

AUDIENCE = 'wakaru-knowledge-delivery'
PREFIX = '/internal/integrations/knowledge-delivery/'
OPERATIONS = ('scope_sync', 'source', 'withdraw_source', 'erase_subject')
ROOT = Path(__file__).resolve().parents[2]/'contracts/knowledge/delivery-v1'
COMMAND = jsonschema.Draft202012Validator(json.loads((ROOT/'command.schema.json').read_text()), format_checker=FORMAT)
LOOKUP = jsonschema.Draft202012Validator(json.loads((ROOT/'lookup.schema.json').read_text()), format_checker=FORMAT)
MAX_BODY = 200_000


class _TransactionStore(KnowledgeStore):
    """Reuse existing operations inside the receipt transaction; never shared across requests."""
    def __init__(self, store, db):
        super().__init__(store.dsn, store.fence_key)
        self.db = db

    @contextmanager
    def transaction(self, **kwargs):
        if kwargs:
            raise StoreConflict('operator_operation_not_allowed')
        yield self.db


def sync_scope(store, db, scope, *, erase=False):
    """Privacy-only advance is permitted; identity never regresses. Advance clears data."""
    tenant, key = store.keys(scope)
    db.execute('INSERT INTO knowledge_retained.tenants VALUES(%s,false) ON CONFLICT DO NOTHING', (tenant,))
    merchant = db.execute('SELECT erased FROM knowledge_retained.tenants WHERE tenant_key=%s FOR SHARE', (tenant,)).fetchone()
    if merchant[0] and not erase:
        raise StoreConflict('merchant_erased')
    row = db.execute('SELECT identity_revision,privacy_generation,data_version,erased,tenant_key FROM knowledge_retained.subjects WHERE scope_key=%s FOR UPDATE', (key,)).fetchone()
    target = (scope['identity_revision'], scope['privacy_generation'])
    if row:
        if row[4] != tenant or target[0] < row[0] or target[1] < row[1] or (target[0] > row[0] and target[1] <= row[1]):
            raise StoreConflict('scope_regression')
        if row[3]:
            if erase:
                db.execute('UPDATE knowledge_retained.subjects SET identity_revision=%s,privacy_generation=%s WHERE scope_key=%s', (*target,key))
                return
            raise StoreConflict('subject_erased')
        # Verify data/fence consistency even for an advance, rather than masking
        # a restored gap by replacing it with an apparently healthy empty row.
        old_scope = dict(scope, identity_revision=row[0], privacy_generation=row[1])
        if not merchant[0]:
            store.bound(db, old_scope)
        if target != row[:2]:
            db.execute('DELETE FROM knowledge_data.subjects WHERE scope_key=%s', (key,))
            version = row[2]+1
            db.execute('UPDATE knowledge_retained.subjects SET identity_revision=%s,privacy_generation=%s,data_version=%s WHERE scope_key=%s', (*target,version,key))
            db.execute('INSERT INTO knowledge_data.subjects VALUES(%s,%s,NULL,%s)', (key,Jsonb(scope),version))
            store.event(db,tenant,key,version,'identity_revision')
    elif not erase:
        store.provision(scope)
    if erase:
        store.erase_subject(scope)


class DeliveryStore:
    def __init__(self, store):
        self.store = store

    def handle(self, raw, *, lookup=False):
        if not isinstance(raw, bytes) or len(raw) > MAX_BODY:
            raise StoreConflict('command_size')
        value = json.loads(raw, object_pairs_hook=_pairs)
        (LOOKUP if lookup else COMMAND).validate(value)
        scope, op = value['scope'], value['operation']
        tenant, key = self.store.keys(scope)
        ckey = self.store.key('delivery-command', value['command_id'])
        sha = value['command_sha256'] if lookup else hashlib.sha256(raw).hexdigest()
        # Serialize receipt insertion and its knowledge mutation with all store
        # operations, including v1 privacy and quarantine. No nested connection.
        with self.store.transaction(exclusive=True) as db:
            old = db.execute('SELECT request_sha256,scope_sha256,operation FROM knowledge_retained.delivery_receipts WHERE scope_key=%s AND command_key=%s', (key,ckey)).fetchone()
            binding = (sha,digest(scope),op)
            if old:
                if old != binding:
                    raise StoreConflict('command_rebound')
                status = 'applied'
            elif lookup:
                # Absence is not proof of non-execution, nor a permission to retry.
                status = 'unknown'
            else:
                if op == 'source':
                    if value['expires_at'] is None:
                        raise StoreConflict('source_deadline_required')
                    deadline = _time(value['expires_at'])
                    now = db.execute('SELECT clock_timestamp()').fetchone()[0]
                    if deadline <= now or deadline > now+timedelta(seconds=30):
                        raise StoreConflict('source_admission_expired')
                elif value['expires_at'] is not None:
                    raise StoreConflict('control_deadline_invalid')
                bound = _TransactionStore(self.store,db)
                if op in ('scope_sync','erase_subject'):
                    if value['arguments']:
                        raise StoreConflict('control_arguments')
                    sync_scope(bound,db,scope,erase=op=='erase_subject')
                else:
                    SERVICE_VALIDATOR.validate(dict(version='knowledge_service_request_v1',operation=op,scope=scope,arguments=value['arguments']))
                    execute(bound,op,scope,value['arguments'])
                if op == 'source' and deadline <= db.execute('SELECT clock_timestamp()').fetchone()[0]:
                    raise StoreConflict('source_admission_expired')
                db.execute('INSERT INTO knowledge_retained.delivery_receipts VALUES(%s,%s,%s,%s,%s)',(key,ckey,*binding))
                status = 'applied'
        return dict(command_id=value['command_id'],command_sha256=sha,operation=op,scope=scope,status=status)


def create_delivery_app(*, enabled=False, grants=None, store=None):
    app = Flask(__name__)
    if not enabled:
        return app
    if store is None:
        raise ValueError('knowledge_store_required')
    verifier = ServiceGrants(AUDIENCE,grants or [])
    allowed = {'POST '+PREFIX+op for op in (*OPERATIONS,'receipt')}
    if any(set(g['operations'])-allowed for g in verifier.grants.values()):
        raise ValueError('delivery_operations_invalid')
    delivery = DeliveryStore(store)

    @app.post(PREFIX+'<operation>')
    def command(operation):
        if operation not in (*OPERATIONS,'receipt'):
            return response({'error':'not_found'},404)
        if request.query_string or (request.content_length or 0)>MAX_BODY:
            return response({'error':'invalid_request'},400)
        raw = request.stream.read(MAX_BODY+1)
        if len(raw)>MAX_BODY:
            return response({'error':'request_too_large'},413)
        principal = verifier.verify(request.headers.get('Authorization',''),'POST',request.path,raw)
        if principal is None:
            return response({'error':'unauthorized'},401)
        try:
            value=json.loads(raw,object_pairs_hook=_pairs)
            (LOOKUP if operation=='receipt' else COMMAND).validate(value)
            if (operation!='receipt' and value['operation']!=operation) or any(value['scope'][k]!=principal[k] for k in ('merchant_id','store_id')):
                return response({'error':'scope_or_operation_binding'},403)
            # Receipt access requires permission for the original operation too.
            if operation=='receipt' and 'POST '+PREFIX+value['operation'] not in verifier.grants[principal['key_id']]['operations']:
                return response({'error':'operation_binding'},403)
            result=delivery.handle(raw,lookup=operation=='receipt')
            return response(dict(version='knowledge_delivery_response_v1',request_sha256=hashlib.sha256(raw).hexdigest(),result=result))
        except (jsonschema.ValidationError,json.JSONDecodeError,UnicodeError,RecursionError):
            return response({'error':'invalid_request'},400)
        except KnowledgeError:
            return response({'error':'delivery_refused'},409)
        except psycopg.Error:
            return response({'error':'delivery_unavailable'},503)
    return app
