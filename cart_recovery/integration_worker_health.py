"""C13 payload-free health. Explicit factory only; no worker or provider startup."""
import json
from datetime import datetime, timedelta, timezone
from flask import jsonify, request
from .integration_invalidation import create_invalidating_service_app
from .integration_service_auth import ServiceGrants

VERSION = "integration_worker_health_c13_v1"
PATH = "/internal/integrations/worker-health"


def worker_health(store, merchant, store_id):
    # Grant authority is supplied by the service verifier. Read no subject data
    # and do not create a missing privacy scope merely to answer a health query.
    with store.connection() as db:
        row = db.execute("SELECT epoch FROM invalidation_scopes WHERE merchant=? AND store=?", (merchant, store_id)).fetchone()
    now = datetime.now(timezone.utc)
    return dict(version=VERSION, merchant_id=merchant, store_id=store_id, service="wakaru",
                observed_at=now.isoformat(), expires_at=(now + timedelta(seconds=30)).isoformat(),
                epoch=row[0] if row else 0, cleanup_state="not_required", pending_since=None,
                oldest_pending_seconds=0, processed_count=0, attempts=0, last_error="",
                last_attempt_at=None, last_success_at=None, worker_seen_at=None,
                worker_state="not_required", next_attempt_at=None, delivery=[],
                live_ingestion_enabled=False, deployment_enabled=False, email_sends_enabled=False)


def create_worker_service_app(*, enabled=False, grants=None, store=None):
    app = create_invalidating_service_app(enabled=enabled, grants=grants, store=store)
    if not enabled:
        return app
    verifier = ServiceGrants("wakaru", grants or [])

    @app.post(PATH)
    def health():
        raw = request.get_data(cache=True)
        principal = verifier.verify(request.headers.get("Authorization", ""), request.method, request.full_path.rstrip("?") if not request.query_string else request.full_path, raw)
        if principal is None or request.query_string:
            return jsonify(error="unauthorized"), 401
        if len(raw) > 16 or raw.strip() != b"{}":
            return jsonify(error="invalid_health_request"), 400
        return jsonify(worker_health(store, principal["merchant_id"], principal["store_id"])), 200

    return app
