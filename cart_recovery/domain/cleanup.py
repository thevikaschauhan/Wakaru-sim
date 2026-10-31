"""Opt-in withdrawal-bound purge receipts. No startup mount or scheduler."""
import hashlib
import json
from pathlib import Path

from flask import Flask, request
import jsonschema
import psycopg
from psycopg.types.json import Jsonb

from cart_recovery.integration_service_auth import ServiceGrants
from .delivery import _TransactionStore
from .knowledge import FORMAT, KnowledgeError, _pairs
from .privacy import purge_withdrawn
from .service import response
from .store import StoreConflict, digest

AUDIENCE = 'wakaru-knowledge-cleanup'
PREFIX = '/internal/integrations/knowledge-cleanup/'
ROOT = Path(__file__).resolve().parents[2] / 'contracts/knowledge/cleanup-v1'
VALIDATOR = jsonschema.Draft202012Validator(json.loads((ROOT/'request.schema.json').read_text()), format_checker=FORMAT)
MAX_BODY = 8192


class CleanupStore:
    def __init__(self, store):
        self.store = store

    def handle(self, raw, *, lookup=False):
        if not isinstance(raw, bytes) or len(raw) > MAX_BODY:
            raise StoreConflict('cleanup_size')
        value = json.loads(raw, object_pairs_hook=_pairs)
        VALIDATOR.validate(value)
        scope = value['scope']
        _, key = self.store.keys(scope)
        command = self.store.key('cleanup-command', value['command_id'])
        sha = hashlib.sha256(raw).hexdigest()
        with self.store.transaction(exclusive=True) as db:
            old = db.execute('SELECT request_sha256,scope_sha256,status,result FROM knowledge_retained.cleanup_receipts '
                             'WHERE scope_key=%s AND command_key=%s', (key, command)).fetchone()
            if old:
                if old[:2] != (sha, digest(scope)):
                    raise StoreConflict('cleanup_rebound')
                status, result = old[2:]
            elif lookup:
                status, result = 'unknown', None
            else:
                # A concrete withdrawal receipt is necessary. Caller-supplied
                # source IDs or an unacknowledged withdrawal cannot authorize purge.
                withdrawal = self.store.key('delivery-command', value['withdrawal_id'])
                receipt = db.execute('SELECT request_sha256,scope_sha256,operation FROM knowledge_retained.delivery_receipts '
                                     'WHERE scope_key=%s AND command_key=%s', (key, withdrawal)).fetchone()
                if receipt != (value['withdrawal_sha256'], digest(scope), 'withdraw_source'):
                    raise StoreConflict('withdrawal_unproven')
                bound = _TransactionStore(self.store, db)
                _, _, _, version = bound.bound(db, scope)
                if version != value['expected_version']:
                    # Durable terminal refusal proves this command did not purge.
                    # An independently refreshed version may use a new command.
                    status = 'refused'
                    result = dict(version=version, removed_sources=0, removed_objects=0, whole_application_complete=False)
                else:
                    result = purge_withdrawn(bound, scope, expected_version=value['expected_version'])
                    status = 'applied'
                db.execute('INSERT INTO knowledge_retained.cleanup_receipts VALUES(%s,%s,%s,%s,%s,%s)',
                           (key, command, sha, digest(scope), status, Jsonb(result)))
        return dict(version='knowledge_cleanup_response_v1', request_sha256=sha, status=status, result=result)


def create_cleanup_app(*, enabled=False, grants=None, store=None):
    app = Flask(__name__)
    if not enabled:
        return app
    if store is None:
        raise ValueError('knowledge_store_required')
    verifier = ServiceGrants(AUDIENCE, grants or [])
    allowed = {'POST '+PREFIX+op for op in ('purge', 'receipt')}
    if any(set(g['operations'])-allowed for g in verifier.grants.values()):
        raise ValueError('cleanup_operations_invalid')
    cleanup = CleanupStore(store)

    @app.post(PREFIX+'<operation>')
    def command(operation):
        if operation not in ('purge', 'receipt'):
            return response({'error':'not_found'}, 404)
        if request.query_string or (request.content_length or 0) > MAX_BODY:
            return response({'error':'invalid_request'}, 400)
        raw = request.stream.read(MAX_BODY+1)
        if len(raw) > MAX_BODY:
            return response({'error':'invalid_request'}, 400)
        principal = verifier.verify(request.headers.get('Authorization',''), 'POST', request.path, raw)
        if principal is None:
            return response({'error':'unauthorized'}, 401)
        try:
            value = json.loads(raw, object_pairs_hook=_pairs)
            VALIDATOR.validate(value)
            if (any(value['scope'][k] != principal[k] for k in ('merchant_id','store_id'))
                    or 'POST '+PREFIX+'purge' not in verifier.grants[principal['key_id']]['operations']):
                return response({'error':'cleanup_binding'}, 403)
            return response(cleanup.handle(raw, lookup=operation=='receipt'))
        except (jsonschema.ValidationError, json.JSONDecodeError, UnicodeError, RecursionError):
            return response({'error':'invalid_request'}, 400)
        except KnowledgeError:
            return response({'error':'cleanup_refused'}, 409)
        except psycopg.Error:
            return response({'error':'cleanup_unavailable'}, 503)
    return app
