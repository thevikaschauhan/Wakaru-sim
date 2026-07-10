"""RQ job body for async cart-recovery (issue #20).

The web tier validates + builds the cart, then enqueues :func:`run_analysis_job`
with ``dataclasses.asdict(cart)``. The worker rebuilds the ``ShopifyCartData``
and calls the same in-process orchestrator the synchronous ``/analyze`` handler
uses (:func:`run_cart_recovery`), so the queued path produces a byte-identical
AbandonmentInsight dict.

PII discipline (issue #7) is load-bearing here and mirrors the ``/analyze``
handler: progress state is PII-free by design, and on failure we re-raise a
sanitized exception with ``from None`` so RQ never stores the original
exception's (potentially PII-bearing) message or chain in ``job.exc_info``.
"""
from __future__ import annotations

import logging

import sentry_sdk
from rq import get_current_job

from cart_recovery.shopify_formatter import ShopifyCartData

from .cart_recovery_workflow import run_cart_recovery
from ..utils.paths import SENTINEL_MERCHANT_ID

logger = logging.getLogger("mirofish.cart_recovery")


class AnalysisJobError(Exception):
    """PII-free failure marker.

    Raised in place of the original exception so RQ's failed-job record carries
    only a type name — never the source exception's args or a chained traceback
    that could echo cart PII.
    """


def run_analysis_job(cart_dict: dict) -> dict:
    """Run the cart-recovery pipeline for one queued job and return the
    AbandonmentInsight dict (the same shape the synchronous ``/analyze`` 200 response builds).

    Progress is written to ``job.meta`` so ``GET /jobs/<id>`` reflects live state
    and survives a web-worker restart (it lives in Redis, not process memory).
    """
    job = get_current_job()
    # The RQ job id is the durable correlation key (replaces /analyze's per-request uuid).
    job_id = job.id if job is not None else "nojob"
    request_id = job_id[:8]
    # merchant_id is bound into the job's meta at enqueue (#24, job->merchant
    # binding); recover it here so the worker's log line carries the same
    # m=<merchant> prefix the web tier uses (the worker has no Flask g).
    merchant_id = (job.meta or {}).get("merchant_id", SENTINEL_MERCHANT_ID) if job is not None else SENTINEL_MERCHANT_ID

    cart = ShopifyCartData(**cart_dict)

    def on_progress(stage: str, state: object) -> None:
        # state dicts are PII-free by design (cart_recovery_workflow keeps
        # customer data out of every on_progress emission).
        if job is None:
            return
        job.meta["stage"] = stage
        job.meta["state"] = state
        job.save_meta()

    try:
        insight = run_cart_recovery(cart, on_progress=on_progress)
    except Exception as e:
        # Mirror the /analyze handler: a sanitized Sentry message (no exception
        # object -> no PII via .args / frame locals) and a type-name-only log.
        sentry_sdk.capture_message(
            f"Cart recovery analysis failed ({type(e).__name__})",
            level="error",
        )
        logger.error(
            f"[{request_id} m={merchant_id}] Cart recovery analysis failed ({type(e).__name__})",
            extra={"request_id": request_id, "job_id": job_id, "merchant_id": merchant_id},
        )
        if job is not None:
            # GET /jobs/<id> reads this — never job.exc_info — so the error
            # surfaced to the caller is PII-free.
            job.meta["error"] = f"Analysis failed ({type(e).__name__})"
            job.save_meta()
        # `from None` drops the original exception's message + chain so RQ's
        # exc_info is just this PII-free marker plus a code-only traceback.
        raise AnalysisJobError(f"Analysis failed ({type(e).__name__})") from None

    return {
        "predicted_reason": insight.predicted_reason,
        "reason_category": insight.reason_category,
        "emotional_state": insight.emotional_state,
        "recommended_angle": insight.recommended_angle,
        "key_objections": insight.key_objections,
        "email_prompt_context": insight.email_prompt_context,
        "confidence": insight.confidence,
        "confidence_reasoning": insight.confidence_reasoning,
    }
