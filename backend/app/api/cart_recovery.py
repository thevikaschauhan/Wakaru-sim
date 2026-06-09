"""
Cart Recovery API Blueprint — Vakaru Integration Point

POST /api/cart-recovery/analyze        synchronous (legacy; blocks ~8-17 min)
POST /api/cart-recovery/jobs           enqueue async analysis -> {job_id, status_url}
GET  /api/cart-recovery/jobs/<job_id>  poll status / progress / result (issue #20)
"""

import sys
import os
import json
import logging
import traceback
import uuid
from dataclasses import asdict

from flask import Blueprint, request, jsonify
import sentry_sdk
from redis.exceptions import RedisError
from rq.exceptions import NoSuchJobError
from rq.job import Job

# Add MiroFish-main root to sys.path so cart_recovery module is importable
_mirofish_root = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
if _mirofish_root not in sys.path:
    sys.path.insert(0, _mirofish_root)

from cart_recovery.shopify_formatter import ShopifyCartData  # noqa: E402

from ..services.cart_recovery_workflow import run_cart_recovery  # noqa: E402
from ..services.cart_recovery_jobs import run_analysis_job  # noqa: E402
from ..services.job_queue import (  # noqa: E402
    analyze_job_timeout,
    get_analyze_queue,
    get_redis_connection,
    FAILURE_TTL_SECONDS,
    RESULT_TTL_SECONDS,
)
from ..extensions import limiter  # noqa: E402
from ..services.idempotency import claim_or_get, record, release  # noqa: E402

logger = logging.getLogger("mirofish.cart_recovery")

cart_recovery_bp = Blueprint("cart_recovery", __name__)


def _paid_rate_limit():
    """Per-IP limit for the paid POSTs (issue #12). Read at request time so the
    ceiling is tunable via CART_RECOVERY_RATE_LIMIT_PER_MIN without a redeploy;
    default 60/min is generous enough not to throttle legitimate cart bursts
    while still capping a compromised key or a runaway loop."""
    return f"{os.environ.get('CART_RECOVERY_RATE_LIMIT_PER_MIN', '60')} per minute"


def _record_idempotency_best_effort(conn, scope, idem_key, value, request_id):
    """Persist the idempotency result without ever letting a cache-write failure
    discard already-completed PAID work (issue #12). The job/analysis has already
    succeeded by the time this is called, so the caller must still get its result.
    A Redis hiccup here only costs the replay-on-retry optimization: a same-key
    retry will 409 on the lingering PENDING marker, or re-run after the 24h TTL."""
    try:
        record(conn, scope, idem_key, value)
    except RedisError:
        logger.error(
            f"[{request_id}] idempotency record failed for scope={scope} "
            f"(paid work already succeeded; returning its result anyway)"
        )


def _build_cart_from_body(request_id):
    """Shared web-tier validation + ShopifyCartData construction.

    Used by both /analyze (sync) and /jobs (async) so the two paths reject the
    same malformed payloads with the same 400s. Returns ``(cart, None)`` on
    success or ``(None, (response, status))`` on a client error: non-JSON body,
    missing required fields, or uncoercible numeric values.
    """
    body = request.get_json(silent=True)
    if not body:
        return None, (jsonify({"success": False, "error": "Request body must be valid JSON"}), 400)

    # Validate required fields
    required = ["customer_id", "email"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return None, (jsonify({
            "success": False,
            "error": f"Missing required fields: {', '.join(missing)}"
        }), 400)

    # Build ShopifyCartData from request body
    try:
        cart = ShopifyCartData(
            customer_id=body.get("customer_id", "unknown"),
            customer_name=body.get("customer_name", "Shopper"),
            email=body.get("email", ""),
            checkout_token=body.get("checkout_token"),
            cart_items=body.get("cart_items", []),
            cart_total=float(body.get("cart_total", 0.0)),
            cart_subtotal=float(body.get("cart_subtotal")) if body.get("cart_subtotal") is not None else None,
            cart_tax=float(body.get("cart_tax")) if body.get("cart_tax") is not None else None,
            currency=body.get("currency", "USD"),
            discount_codes=body.get("discount_codes", []),
            discount_amount=float(body.get("discount_amount")) if body.get("discount_amount") is not None else None,
            discount_type=body.get("discount_type"),
            shipping_cost=float(body.get("shipping_cost")) if body.get("shipping_cost") is not None else None,
            shipping_method=body.get("shipping_method"),
            shipping_country=body.get("shipping_country"),
            payment_gateway_attempted=body.get("payment_gateway_attempted"),
            payment_method_type=body.get("payment_method_type"),
            browsing_history=body.get("browsing_history", []),
            collections_viewed=body.get("collections_viewed", []),
            products_viewed=body.get("products_viewed", []),
            products_removed=body.get("products_removed", []),
            searches_submitted=body.get("searches_submitted", []),
            alert_messages_shown=body.get("alert_messages_shown", []),
            time_on_site_minutes=float(body.get("time_on_site_minutes", 0.0)),
            exit_page=body.get("exit_page", ""),
            abandoned_at_step=body.get("abandoned_at_step", ""),
            device_type=body.get("device", body.get("device_type", "unknown")),
            viewport_width=int(body.get("viewport_width")) if body.get("viewport_width") is not None else None,
            language=body.get("language"),
            market=body.get("market"),
            referral_source=body.get("referral_source", ""),
            utm_campaign=body.get("utm_campaign"),
            utm_source=body.get("utm_source"),
            past_orders=int(body.get("past_orders", 0)),
            total_spend_lifetime=float(body.get("total_spend_lifetime")) if body.get("total_spend_lifetime") is not None else None,
            is_first_order=bool(body.get("is_first_order", False)),
            customer_tags=body.get("customer_tags", []),
            email_marketing_consent=body.get("email_marketing_consent"),
            location=body.get("location", ""),
            hours_since_last_abandonment=float(body.get("hours_since_last_abandonment")) if body.get("hours_since_last_abandonment") is not None else None,
            # PIE-V2 enrichment fields
            shopper_profile=body.get("shopper_profile"),
            behavioral_memory=body.get("behavioral_memory", ""),
            form_interactions=body.get("form_interactions", []),
            hover_signals=body.get("hover_signals", []),
            ontology_hint=body.get("ontology_hint"),
            recovery_history=body.get("recovery_history", []),
            merchant_effectiveness=body.get("merchant_effectiveness"),
        )
    except (TypeError, ValueError) as e:
        # Don't interpolate `e` into the log: float()/int() errors echo the
        # offending value verbatim, which could be a PII field a caller
        # mistakenly put in a numeric slot (e.g. cart_total). The exception
        # type is enough for log triage; the full message is still returned
        # in the 400 response so the caller can fix their payload.
        logger.warning(f"[{request_id}] Invalid cart data ({type(e).__name__})")
        return None, (jsonify({"success": False, "error": f"Invalid cart data: {str(e)}"}), 400)

    return cart, None


@cart_recovery_bp.route("/analyze", methods=["POST"])
@limiter.limit(_paid_rate_limit)
def analyze():
    """
    Analyze a cart abandonment event synchronously (legacy path).

    Input JSON: ShopifyCartData fields
    Output JSON: { "success": true, "data": AbandonmentInsight }

    Blocks for the full ~8-17 min pipeline. New callers should prefer the async
    POST /jobs + GET /jobs/<id> path (issue #20).
    """
    # Short per-request correlation id for log triage.
    # See issue #7; a proper request-id middleware will land with Phase 3.
    request_id = uuid.uuid4().hex[:8]

    cart, error = _build_cart_from_body(request_id)
    if error is not None:
        return error

    # Idempotency (issue #12): a repeated POST with the same Idempotency-Key must
    # not run a second paid pipeline. Replay the cached result; reject a request
    # that arrives while the first is still running.
    idem_key = request.headers.get("Idempotency-Key")
    idem_conn = get_redis_connection() if idem_key else None
    if idem_conn is not None:
        state, existing = claim_or_get(idem_conn, "analyze", idem_key)
        if state == "replay":
            return jsonify({"success": True, "data": json.loads(existing), "replayed": True}), 200
        if state == "pending":
            return jsonify({
                "success": False,
                "error": "An analysis for this Idempotency-Key is already running",
            }), 409

    # Progress logging callback
    def on_progress(stage: str, state: object):
        logger.info(f"[{request_id}] {stage}: {state}")

    # Run analysis
    try:
        insight = run_cart_recovery(cart, on_progress=on_progress)
    except Exception as e:
        # The paid work ran but failed: free the idempotency slot so the caller
        # can retry (there is no cached result to replay). This is a positive
        # failure, not an ambiguous outcome, so releasing is safe.
        if idem_conn is not None:
            release(idem_conn, "analyze", idem_key)
        # Don't send the exception object to Sentry: its .args (and frame
        # locals via the traceback) can carry PII from cart data into the
        # event payload, which _scrub_pii does not cover. Send a sanitized
        # message instead.
        sentry_sdk.capture_message(
            f"Cart recovery analysis failed ({type(e).__name__})",
            level="error",
        )
        logger.error(f"[{request_id}] Cart recovery analysis failed ({type(e).__name__})")
        logger.debug(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": f"Analysis failed: {str(e)}"
        }), 500

    data = {
        "predicted_reason": insight.predicted_reason,
        "emotional_state": insight.emotional_state,
        "recommended_angle": insight.recommended_angle,
        "key_objections": insight.key_objections,
        "email_prompt_context": insight.email_prompt_context,
        "confidence": insight.confidence,
        "confidence_reasoning": insight.confidence_reasoning,
    }
    if idem_conn is not None:
        _record_idempotency_best_effort(idem_conn, "analyze", idem_key, json.dumps(data), request_id)
    return jsonify({"success": True, "data": data}), 200


@cart_recovery_bp.route("/jobs", methods=["POST"])
@limiter.limit(_paid_rate_limit)
def enqueue_job():
    """
    Enqueue a cart abandonment analysis and return immediately (issue #20).

    Input JSON: ShopifyCartData fields (same as /analyze).
    Output JSON (202): { success, job_id, status_url }

    The cheap web-tier validation runs inline (same 400s as /analyze); the
    ~8-17 min pipeline runs on a separate RQ worker. Poll GET /jobs/<id> for
    progress and the terminal 7-field result.

    Idempotent on the Idempotency-Key header (issue #12): a repeated POST with
    the same key replays the original job_id instead of enqueuing a second paid
    job. The caller (the engine) sends a per-attempt-stable key.
    """
    request_id = uuid.uuid4().hex[:8]

    cart, error = _build_cart_from_body(request_id)
    if error is not None:
        return error

    # Idempotency claim (issue #12): if a job for this key already exists, replay
    # its id; if one is mid-enqueue, reject. Otherwise we hold the slot and must
    # record the job id (on success) or release it (if no job was created).
    idem_key = request.headers.get("Idempotency-Key")
    idem_conn = get_redis_connection() if idem_key else None
    if idem_conn is not None:
        state, existing = claim_or_get(idem_conn, "jobs", idem_key)
        if state == "replay":
            return jsonify({
                "success": True,
                "job_id": existing,
                "status_url": f"/api/cart-recovery/jobs/{existing}",
                "replayed": True,
            }), 202
        if state == "pending":
            return jsonify({
                "success": False,
                "error": "A job for this Idempotency-Key is already being created",
            }), 409

    queue = get_analyze_queue()
    if queue is None:
        # REDIS_URL unset: the async path is unavailable, but /analyze is not.
        # No job was created — free the idempotency slot so a retry can proceed.
        if idem_conn is not None:
            release(idem_conn, "jobs", idem_key)
        logger.error(f"[{request_id}] Job queue unavailable (REDIS_URL not configured)")
        return jsonify({"success": False, "error": "Job queue unavailable"}), 503

    try:
        # No auto-retry (RQ default): an 8-17 min, paid LLM job must not silently
        # re-run. Cross-request dedup is the Idempotency-Key above. A failed job
        # is terminal — GET surfaces status=failed and the caller decides.
        job = queue.enqueue(
            run_analysis_job,
            asdict(cart),
            # PII-safe: RQ's default job description renders the call args
            # (the cart dict — customer_id/name/email) into a string stored in
            # Redis and logged at dequeue. Pin an explicit PII-free description.
            description="cart-recovery analysis",
            job_timeout=analyze_job_timeout(),
            result_ttl=RESULT_TTL_SECONDS,
            failure_ttl=FAILURE_TTL_SECONDS,
        )
    except RedisError:
        # Enqueue failed → no job was created → free the slot for a retry.
        if idem_conn is not None:
            release(idem_conn, "jobs", idem_key)
        logger.error(f"[{request_id}] Job enqueue failed (Redis unreachable)")
        return jsonify({"success": False, "error": "Job queue unavailable"}), 503

    if idem_conn is not None:
        _record_idempotency_best_effort(idem_conn, "jobs", idem_key, job.id, request_id)

    return jsonify({
        "success": True,
        "job_id": job.id,
        "status_url": f"/api/cart-recovery/jobs/{job.id}",
    }), 202


@cart_recovery_bp.route("/jobs/<job_id>", methods=["GET"])
def job_status(job_id):
    """
    Return status / progress / result for an enqueued analysis job (issue #20).

    Output JSON (200): { success, job_id, status, progress, result?, error? }
      - status:   queued | started | finished | failed | deferred | ...
      - progress: latest { stage, state } the worker recorded (PII-free)
      - result:   the 7-field AbandonmentInsight dict, when status == finished
      - error:    a PII-free failure marker, when status == failed
    """
    request_id = uuid.uuid4().hex[:8]

    connection = get_redis_connection()
    if connection is None:
        logger.error(f"[{request_id}] Job queue unavailable (REDIS_URL not configured)")
        return jsonify({"success": False, "error": "Job queue unavailable"}), 503

    try:
        job = Job.fetch(job_id, connection=connection)
    except NoSuchJobError:
        return jsonify({"success": False, "error": "Job not found"}), 404
    except RedisError:
        logger.error(f"[{request_id}] Job fetch failed (Redis unreachable)")
        return jsonify({"success": False, "error": "Job queue unavailable"}), 503

    status = job.get_status()
    payload = {
        "success": True,
        "job_id": job.id,
        "status": status,
        "progress": job.meta or {},
    }
    if status == "finished":
        payload["result"] = job.result
    elif status == "failed":
        # NEVER return job.exc_info (full traceback — may carry PII). The worker
        # writes a PII-free marker to job.meta["error"]; fall back to a generic.
        payload["error"] = (job.meta or {}).get("error", "Analysis failed")

    return jsonify(payload), 200
