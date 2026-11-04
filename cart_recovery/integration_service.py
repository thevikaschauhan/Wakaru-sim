"""Opt-in private analysis service. Default-off; no ingestion, models or sender.

The host must inject an explicitly owned persistent PrivacyStore and per-store
service grants. No environment keys or legacy merchant credentials are inherited.
"""
import json
from flask import Flask, jsonify
from .bounded_context_api import create_blueprint
from .intelligence.bounded_context import BoundedService
from .intelligence.integration_contracts import _pairs
from .integration_service_auth import MAX_BODY, ServiceGrants


def create_service_app(*, enabled=False, grants=None, store=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
    if not enabled:
        return app
    if store is None:
        raise ValueError("integration_store_required")
    verifier = ServiceGrants("wakaru", grants or [])

    def authorize(request):
        # Only synchronous bounded interpretation is exposed in this slice.
        # Existing async paths remain unregistered to the grant verifier.
        if request.path != "/internal/integrations/analyze" or request.query_string:
            return None
        raw = request.get_data(cache=True)
        p = verifier.verify(request.headers.get("Authorization", ""), request.method, request.path, raw)
        if p is None:
            return None
        try:
            value = json.loads(raw, object_pairs_hook=_pairs)
            context = value["context"]
            customer = context["customer_context"]
            if context["merchant_id"] != p["merchant_id"] or customer["store_id"] != p["store_id"]:
                return None
            return p["merchant_id"]
        except (ValueError, KeyError, TypeError):
            return None

    app.register_blueprint(create_blueprint(BoundedService(store), authorize), url_prefix="/internal/integrations")

    @app.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error="integration_request_too_large"), 413

    return app
