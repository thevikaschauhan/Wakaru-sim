"""Optional private C8 HTTP adapter. Not mounted by production bootstrap.

The host must supply existing service authentication and an authoritative
merchant binding. Queue execution is explicit; this module starts no workers.
"""
from datetime import datetime, timezone
import json
from flask import Blueprint, jsonify, request, g

from .intelligence.integration_contracts import _pairs
from .integration_privacy import PrivacyStop


def create_blueprint(service, authorize):
    bp = Blueprint("bounded_context_c8", __name__)

    @bp.before_request
    def authenticate():
        request.max_content_length = 200000
        if service is None or authorize is None:
            return jsonify(error="bounded_context_disabled"), 503
        g.bounded_merchant = authorize(request)
        if not g.bounded_merchant:
            return jsonify(error="unauthorized"), 401

    @bp.after_request
    def prevent_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    def binding():
        # authorize verifies the signed full body and resolves tenant identity.
        merchant = g.bounded_merchant
        raw = request.get_data(cache=True)
        if len(raw) > 200000:
            raise ValueError("context_bounds")
        value = json.loads(raw, object_pairs_hook=_pairs)
        context = value["context"]
        customer = context["customer_context"]
        return raw, dict(merchant=merchant, store=customer["store_id"], subject=customer["subject_id"], episode=context["episode_id"], now=datetime.now(timezone.utc))

    @bp.post("/analyze")
    def analyze():
        try:
            raw, bound = binding()
            return jsonify(plan=service.analyze(raw, **bound)), 200
        except Exception:
            # No provider text, tracebacks or body excerpts leave this boundary.
            return jsonify(error="bounded_context_rejected"), 422

    @bp.post("/jobs")
    def enqueue():
        try:
            raw, bound = binding()
            job = service.submit(raw, **bound)
            return jsonify(job_id=job, store_id=bound["store"], status="queued"), 202
        except Exception:
            return jsonify(error="bounded_context_rejected"), 422

    @bp.get("/jobs/<store>/<job>")
    def poll(store, job):
        try:
            plan = service.result(g.bounded_merchant, store, job, now=datetime.now(timezone.utc))
            return jsonify(status="finished", plan=plan), 200
        except PrivacyStop:
            try:
                service.store.read(g.bounded_merchant, store, job, now=datetime.now(timezone.utc).timestamp())
                return jsonify(status="queued", job_id=job), 200
            except Exception:
                return jsonify(error="result_unavailable"), 404
        except Exception:
            return jsonify(error="result_unavailable"), 404

    return bp
