"""C12 store-wide context withdrawal. Explicit factory only; no background work."""
import json
import re
from flask import jsonify, request
from .integration_service import create_service_app
from .integration_service_auth import ServiceGrants
from .intelligence.integration_contracts import _pairs

VERSION = "integration_invalidation_c12_v1"
PATH = "/internal/integrations/invalidate"
MAX_INTEGER = 9007199254740991


def validate_event(value, merchant, store):
    if (not isinstance(value, dict) or set(value) != {"version", "merchant_id", "store_id", "epoch", "cutoff_us"}
            or value["version"] != VERSION or value["merchant_id"] != merchant or value["store_id"] != store
            or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", merchant)
            or not re.fullmatch(r"[1-9][0-9]{0,9}", store)
            or any(type(value[k]) is not int or not 1 <= value[k] <= MAX_INTEGER for k in ("epoch", "cutoff_us"))):
        raise ValueError("invalidation_invalid")


def create_invalidating_service_app(*, enabled=False, grants=None, store=None):
    app = create_service_app(enabled=enabled, grants=grants, store=store)
    if not enabled:
        return app
    verifier = ServiceGrants("wakaru", grants or [])

    @app.post(PATH)
    def invalidate():
        raw = request.get_data(cache=True)
        principal = verifier.verify(request.headers.get("Authorization", ""), request.method, request.full_path.rstrip("?") if not request.query_string else request.full_path, raw)
        if principal is None or request.query_string:
            return jsonify(error="unauthorized"), 401
        try:
            if len(raw) > 2048:
                raise ValueError("invalidation_bounds")
            value = json.loads(raw, object_pairs_hook=_pairs)
            receipt = store.apply_invalidation(principal["merchant_id"], principal["store_id"], value)
            return jsonify(receipt), 200
        except (ValueError, TypeError, KeyError):
            return jsonify(error="invalidation_rejected"), 409
    return app
