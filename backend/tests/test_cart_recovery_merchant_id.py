"""Issue #24 (CP1) — merchant_id binding on the live cart-recovery path.

resolve_merchant_id (app factory) binds g.merchant_id from the X-Merchant-Id
header the engine sends, validated to a path-safe UUID. A request with no header
is bucketed under the nil-UUID sentinel (so Wakaru can deploy before the engine
starts sending it); a present-but-malformed id fails loud with 400. The bound
merchant_id then namespaces the idempotency scope (no cross-tenant replay), the
rate-limit bucket, and the cart-recovery log prefix. Synthetic UUIDs only.
"""
import logging
from types import SimpleNamespace

import fakeredis
from rq import Queue

from app.extensions import _merchant_or_ip_key
from app.services.job_queue import ANALYZE_QUEUE_NAME
from app.utils.paths import SENTINEL_MERCHANT_ID

MERCHANT_A = "11111111-1111-4111-8111-111111111111"
MERCHANT_B = "22222222-2222-4222-8222-222222222222"

VALID_PAYLOAD = {
    "customer_id": "cust_test",
    "email": "shopper@example.com",
    "cart_items": [{"product": "x", "price": 1.0, "quantity": 1}],
    "cart_total": 1.0,
}


def _wire(monkeypatch, conn, queue):
    monkeypatch.setattr(
        "app.api.cart_recovery.get_analyze_queue", lambda connection=None: queue
    )
    monkeypatch.setattr("app.api.cart_recovery.get_redis_connection", lambda: conn)


def _fake_insight():
    return SimpleNamespace(
        predicted_reason="stub reason",
        reason_category="unknown",
        emotional_state="anxious",
        recommended_angle="discount-or-value",
        key_objections=[],
        email_prompt_context="",
        confidence=0.5,
        confidence_reasoning="",
    )


def _fake_run_with_progress(cart, on_progress=None):
    # Fire the progress callback so the INFO log line (which carries the
    # merchant_id prefix under test) is emitted, then return a stub insight.
    if on_progress is not None:
        on_progress("preparing", "stub progress")
    return _fake_insight()


# --- resolve_merchant_id: X-Merchant-Id header → g.merchant_id ----------------

def test_malformed_merchant_id_header_rejected_400(client):
    # The engine controls the header value, so a non-UUID is a bug/attack, not a
    # legacy caller — fail loud at the request boundary (before the handler runs).
    resp = client.post(
        "/api/cart-recovery/jobs",
        json=VALID_PAYLOAD,
        headers={"X-Merchant-Id": "not-a-uuid"},
    )
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "invalid_merchant_id"


def test_path_traversal_merchant_id_header_rejected_400(client):
    # merchant_id becomes a filesystem path component (CP2); a traversal payload
    # in the header must never reach a join — it is rejected as a non-UUID.
    resp = client.post(
        "/api/cart-recovery/jobs",
        json=VALID_PAYLOAD,
        headers={"X-Merchant-Id": "../../../etc/passwd"},
    )
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "invalid_merchant_id"


def test_valid_merchant_id_bound_to_log_prefix(client, monkeypatch, caplog):
    monkeypatch.setattr(
        "app.api.cart_recovery.run_cart_recovery", _fake_run_with_progress
    )
    with caplog.at_level(logging.INFO, logger="mirofish.cart_recovery"):
        resp = client.post(
            "/api/cart-recovery/analyze",
            json=VALID_PAYLOAD,
            headers={"X-Merchant-Id": MERCHANT_A},
        )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    progress = [r.getMessage() for r in caplog.records if "stub progress" in r.getMessage()]
    assert progress, "expected a progress log line"
    assert f"m={MERCHANT_A}" in progress[0], progress[0]


def test_missing_merchant_id_falls_back_to_sentinel(client, monkeypatch, caplog):
    # No X-Merchant-Id (legacy /analyze caller, or pre-deploy engine) must not
    # 400 — it is bucketed under the nil-UUID sentinel so the route still runs.
    monkeypatch.setattr(
        "app.api.cart_recovery.run_cart_recovery", _fake_run_with_progress
    )
    with caplog.at_level(logging.INFO, logger="mirofish.cart_recovery"):
        resp = client.post("/api/cart-recovery/analyze", json=VALID_PAYLOAD)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    progress = [r.getMessage() for r in caplog.records if "stub progress" in r.getMessage()]
    assert progress, "expected a progress log line"
    assert f"m={SENTINEL_MERCHANT_ID}" in progress[0], progress[0]


# --- idempotency scope is namespaced by merchant (no cross-tenant replay) ------

def test_same_idem_key_different_merchants_do_not_collide(client, monkeypatch):
    conn = fakeredis.FakeStrictRedis()
    queue = Queue(ANALYZE_QUEUE_NAME, connection=conn)  # is_async: jobs stay queued
    _wire(monkeypatch, conn, queue)
    key = {"Idempotency-Key": "shared-key"}

    r_a = client.post(
        "/api/cart-recovery/jobs", json=VALID_PAYLOAD,
        headers={**key, "X-Merchant-Id": MERCHANT_A},
    )
    r_b = client.post(
        "/api/cart-recovery/jobs", json=VALID_PAYLOAD,
        headers={**key, "X-Merchant-Id": MERCHANT_B},
    )
    assert r_a.status_code == 202 and r_b.status_code == 202, (r_a.status_code, r_b.status_code)
    # Same Idempotency-Key, different merchant → distinct scope → NOT a replay.
    assert r_a.get_json()["job_id"] != r_b.get_json()["job_id"]
    assert r_b.get_json().get("replayed") is not True
    assert len(queue.job_ids) == 2  # one paid job per tenant

    # Same merchant + same key → replay the original job, no new enqueue.
    r_a2 = client.post(
        "/api/cart-recovery/jobs", json=VALID_PAYLOAD,
        headers={**key, "X-Merchant-Id": MERCHANT_A},
    )
    assert r_a2.get_json()["job_id"] == r_a.get_json()["job_id"]
    assert r_a2.get_json().get("replayed") is True
    assert len(queue.job_ids) == 2  # a's replay added nothing


# --- rate-limit key is per-merchant -------------------------------------------

def test_rate_limit_key_is_per_merchant(app):
    from flask import g
    with app.test_request_context("/api/cart-recovery/jobs"):
        g.merchant_id = MERCHANT_A
        assert _merchant_or_ip_key() == f"merchant:{MERCHANT_A}"


def test_rate_limit_key_falls_back_to_ip_without_merchant(app):
    with app.test_request_context("/health"):
        # No merchant bound (resolve_merchant_id only fires on /api/cart-recovery)
        # → per-IP bucket, never a merchant bucket.
        assert not _merchant_or_ip_key().startswith("merchant:")
