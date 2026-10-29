"""Explicit factory for privileged Engine knowledge commands. Never mounted at startup."""
import base64
import binascii
import hashlib
import json
from pathlib import Path

from flask import Flask, Response, request
import jsonschema
import psycopg

from cart_recovery.integration_service_auth import MAX_BODY, ServiceGrants
from .knowledge import FORMAT, KnowledgeError, _pairs

AUDIENCE = 'wakaru-knowledge'
PREFIX = '/internal/integrations/knowledge/'
OPERATIONS = ('provision', 'source', 'entity', 'save', 'current', 'export_subject',
              'erase_subject', 'withdraw_source', 'revise_identity')
SCHEMA = json.loads((Path(__file__).resolve().parents[2]/'contracts/knowledge/service-v1/request.schema.json').read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA, format_checker=FORMAT)
MAX_RESPONSE = 2_000_000


def response(value, status=200):
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > MAX_RESPONSE:
        raw, status = b'{"error":"response_too_large"}', 413
    return Response(raw, status=status, content_type='application/json', headers={'Cache-Control':'no-store'})


def execute(store, operation, scope, args):
    # No dynamic getattr: bootstrap, merchant-wide access and restore control
    # intentionally have no remotely callable operation.
    if operation == 'provision':
        store.provision(scope)
    elif operation == 'source':
        try:
            content = base64.b64decode(args['content_base64'], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise KnowledgeError('invalid_content') from exc
        store.capture_source(scope, args['receipt'], content)
    elif operation == 'entity':
        store.capture_entity(scope, args['receipt'])
    elif operation == 'save':
        return store.save(json.dumps(args['payload']), scope=scope,
                          command_id=args['command_id'], expected_version=args['expected_version'])
    elif operation == 'current':
        return store.current(scope)
    elif operation == 'export_subject':
        return store.export_subject(scope)
    elif operation == 'erase_subject':
        store.erase_subject(scope)
    elif operation == 'withdraw_source':
        return {'version':store.withdraw_source(scope, args['source_ref'])}
    elif operation == 'revise_identity':
        store.revise_identity(scope, args['new_scope'])
    else:
        raise KnowledgeError('unknown_operation')
    return {'acknowledged':True}


def create_knowledge_service_app(*, enabled=False, grants=None, store=None):
    app = Flask(__name__)
    if not enabled:
        return app
    if store is None:
        raise ValueError('knowledge_store_required')
    verifier = ServiceGrants(AUDIENCE, grants or [])
    allowed = {'POST '+PREFIX+op for op in OPERATIONS}
    if any(set(g['operations']) - allowed for g in verifier.grants.values()):
        raise ValueError('knowledge_operations_invalid')

    @app.post(PREFIX+'<operation>')
    def command(operation):
        if operation not in OPERATIONS:
            return response({'error':'not_found'}, 404)
        if request.query_string or (request.content_length or 0) > MAX_BODY:
            return response({'error':'invalid_request'}, 400)
        raw = request.stream.read(MAX_BODY+1)
        if len(raw) > MAX_BODY:
            return response({'error':'request_too_large'}, 413)
        principal = verifier.verify(request.headers.get('Authorization',''), 'POST', request.path, raw)
        if principal is None:
            return response({'error':'unauthorized'}, 401)
        try:
            value = json.loads(raw, object_pairs_hook=_pairs)
            VALIDATOR.validate(value)
            scope = value['scope']
            if value['operation'] != operation or any(scope[k] != principal[k] for k in ('merchant_id','store_id')):
                return response({'error':'scope_or_operation_binding'}, 403)
            result = execute(store, operation, scope, value['arguments'])
            return response({'version':'knowledge_service_response_v1','operation':operation,
                             'scope':scope,'request_sha256':hashlib.sha256(raw).hexdigest(),'result':result})
        except (jsonschema.ValidationError, json.JSONDecodeError, UnicodeError, RecursionError):
            return response({'error':'invalid_request'}, 400)
        except KnowledgeError:
            # Do not turn exception text, raw receipts or SQL errors into logs or responses.
            return response({'error':'knowledge_refused'}, 409)
        except psycopg.Error:
            return response({'error':'knowledge_unavailable'}, 503)

    return app
