"""Opt-in privacy transport. Never mounted or granted permission at startup."""
import hashlib
import json
from pathlib import Path

from flask import Flask, request
import jsonschema
import psycopg

from cart_recovery.integration_service_auth import ServiceGrants
from .knowledge import FORMAT, KnowledgeError, _pairs
from .privacy import export_retained_page, purge_withdrawn
from .service import response

AUDIENCE = 'wakaru-knowledge-privacy'
PREFIX = '/internal/integrations/knowledge-privacy/'
OPERATIONS = ('purge_withdrawn', 'export_retained_page')
MAX_REQUEST = 16000
SCHEMA = json.loads((Path(__file__).resolve().parents[2]/'contracts/knowledge/privacy-v1/request.schema.json').read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA, format_checker=FORMAT)


def create_privacy_app(*, enabled=False, grants=None, store=None):
    app = Flask(__name__)
    if not enabled:
        return app
    if store is None:
        raise ValueError('knowledge_store_required')
    verifier = ServiceGrants(AUDIENCE, grants or [])
    allowed = {'POST '+PREFIX+op for op in OPERATIONS}
    if any(set(g['operations'])-allowed for g in verifier.grants.values()):
        raise ValueError('privacy_operations_invalid')

    @app.post(PREFIX+'<operation>')
    def command(operation):
        if operation not in OPERATIONS:
            return response({'error':'not_found'}, 404)
        if request.query_string or (request.content_length or 0) > MAX_REQUEST:
            return response({'error':'invalid_request'}, 400)
        raw = request.stream.read(MAX_REQUEST+1)
        if len(raw) > MAX_REQUEST:
            return response({'error':'request_too_large'}, 413)
        principal = verifier.verify(request.headers.get('Authorization',''), 'POST', request.path, raw)
        if principal is None:
            return response({'error':'unauthorized'}, 401)
        try:
            value = json.loads(raw, object_pairs_hook=_pairs)
            VALIDATOR.validate(value)
            scope, args = value['scope'], value['arguments']
            if value['operation'] != operation or any(scope[k] != principal[k] for k in ('merchant_id','store_id')):
                return response({'error':'scope_or_operation_binding'}, 403)
            if operation == 'purge_withdrawn':
                result = purge_withdrawn(store, scope, expected_version=args['expected_version'])
            else:
                result = export_retained_page(store, scope, **args)
            return response({'version':'knowledge_privacy_response_v1', 'operation':operation,
                             'scope':scope, 'request_sha256':hashlib.sha256(raw).hexdigest(), 'result':result})
        except (jsonschema.ValidationError, json.JSONDecodeError, UnicodeError, RecursionError):
            return response({'error':'invalid_request'}, 400)
        except KnowledgeError:
            return response({'error':'privacy_refused'}, 409)
        except psycopg.Error:
            return response({'error':'privacy_unavailable'}, 503)
    return app
