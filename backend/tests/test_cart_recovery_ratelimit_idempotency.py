"""Issue #12 — rate limiting + Idempotency-Key on the paid cart-recovery routes.

Rate limiting: a generous per-IP cap on the paid POSTs (defense-in-depth against
a compromised key / runaway loop; auth #10 already blocks anonymous abuse). The
limit-per-minute is env-tunable so this test can drive it low.

Idempotency: a repeated POST with the same Idempotency-Key must not run a second
paid pipeline — /jobs replays the same job_id (enqueues once); /analyze replays
the cached result (runs the pipeline once). Without a key, behaviour is unchanged.

fakeredis backs both the queue and the idempotency store. Synthetic data only.
"""
from types import SimpleNamespace

import fakeredis
from redis.exceptions import RedisError
from rq import Queue

from app import create_app
from app.services.job_queue import ANALYZE_QUEUE_NAME
from tests.conftest import TEST_MERCHANT_ID, TEST_WAKARU_API_KEY, SigningFlaskClient

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
    monkeypatch.setattr(
        "app.api.cart_recovery.get_redis_connection", lambda: conn
    )


def _fake_insight():
    return SimpleNamespace(
        predicted_reason="Shipping cost shock",
        reason_category="shipping_cost",
        emotional_state="price-sensitive",
        recommended_angle="discount-or-value",
        key_objections=["$18 shipping"],
        email_prompt_context="Offer free shipping",
        confidence=0.7,
        confidence_reasoning="clear exit at shipping step",
    )


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

def test_rate_limit_429_after_threshold(monkeypatch):
    # Drive the limit low via env; in-memory storage (REDIS_URL unset in tests).
    monkeypatch.setenv("CART_RECOVERY_RATE_LIMIT_PER_MIN", "2")
    app = create_app()
    app.config["TESTING"] = True
    app.test_client_class = SigningFlaskClient  # pass #11 HMAC (body-signed POSTs)
    client = app.test_client()
    client.environ_base["HTTP_X_API_KEY"] = TEST_WAKARU_API_KEY  # pass #10 auth
    client.environ_base["HTTP_X_MERCHANT_ID"] = TEST_MERCHANT_ID  # pass #24 merchant gate

    # The limiter checks before the view, so even a 503 (queue unavailable) counts.
    statuses = [
        client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD).status_code
        for _ in range(3)
    ]
    assert statuses[2] == 429, statuses
    resp = client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD)
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") is not None


def test_poll_get_is_not_rate_limited(monkeypatch):
    # The engine polls frequently; the GET poll must not be throttled.
    monkeypatch.setenv("CART_RECOVERY_RATE_LIMIT_PER_MIN", "2")
    app = create_app()
    app.config["TESTING"] = True
    app.test_client_class = SigningFlaskClient  # GETs pass through unsigned
    conn = fakeredis.FakeStrictRedis()
    monkeypatch.setattr("app.api.cart_recovery.get_redis_connection", lambda: conn)
    client = app.test_client()
    client.environ_base["HTTP_X_API_KEY"] = TEST_WAKARU_API_KEY
    client.environ_base["HTTP_X_MERCHANT_ID"] = TEST_MERCHANT_ID  # pass #24 merchant gate
    statuses = [
        client.get("/api/cart-recovery/jobs/none").status_code for _ in range(5)
    ]
    assert all(s != 429 for s in statuses), statuses


# --------------------------------------------------------------------------
# Idempotency — /jobs (enqueue once, replay the job_id)
# --------------------------------------------------------------------------

def test_jobs_same_idempotency_key_enqueues_once(client, monkeypatch):
    conn = fakeredis.FakeStrictRedis()
    queue = Queue(ANALYZE_QUEUE_NAME, connection=conn)  # is_async=True: stays queued
    _wire(monkeypatch, conn, queue)
    headers = {"Idempotency-Key": "idem-jobs-1"}
    r1 = client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD, headers=headers)
    r2 = client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD, headers=headers)
    assert r1.status_code == 202 and r2.status_code == 202, (r1.status_code, r2.status_code)
    assert r1.get_json()["job_id"] == r2.get_json()["job_id"]
    assert r2.get_json().get("replayed") is True
    assert len(queue.job_ids) == 1  # exactly one paid job enqueued


def test_jobs_without_idempotency_key_enqueues_each_time(client, monkeypatch):
    conn = fakeredis.FakeStrictRedis()
    queue = Queue(ANALYZE_QUEUE_NAME, connection=conn)
    _wire(monkeypatch, conn, queue)
    client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD)
    client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD)
    assert len(queue.job_ids) == 2  # no key -> no dedup (unchanged behaviour)


# --------------------------------------------------------------------------
# Idempotency — /analyze (run once, replay the cached result)
# --------------------------------------------------------------------------

def test_analyze_same_idempotency_key_runs_pipeline_once(client, monkeypatch):
    conn = fakeredis.FakeStrictRedis()
    monkeypatch.setattr("app.api.cart_recovery.get_redis_connection", lambda: conn)
    calls = []

    def fake_run(cart, on_progress=None):
        calls.append(1)
        return _fake_insight()

    monkeypatch.setattr("app.api.cart_recovery.run_cart_recovery", fake_run)
    headers = {"Idempotency-Key": "idem-analyze-1"}
    r1 = client.post("/api/cart-recovery/analyze", json=VALID_PAYLOAD, headers=headers)
    r2 = client.post("/api/cart-recovery/analyze", json=VALID_PAYLOAD, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(calls) == 1  # pipeline ran exactly once
    assert r2.get_json().get("replayed") is True
    assert r1.get_json()["data"] == r2.get_json()["data"]


# --------------------------------------------------------------------------
# record() failure after the paid work must NOT lose the result (best-effort
# cache write — the authoritative paid outcome is still returned).
# --------------------------------------------------------------------------

def test_jobs_record_failure_still_returns_job_id(client, monkeypatch):
    conn = fakeredis.FakeStrictRedis()
    queue = Queue(ANALYZE_QUEUE_NAME, connection=conn)
    _wire(monkeypatch, conn, queue)

    def boom(*a, **k):
        raise RedisError("redis blip after enqueue")

    monkeypatch.setattr("app.api.cart_recovery.record", boom)
    resp = client.post(
        "/api/cart-recovery/jobs", json=VALID_PAYLOAD, headers={"Idempotency-Key": "idem-rec-1"}
    )
    # The paid job was created; a failed cache write must not 500 away its id.
    assert resp.status_code == 202, resp.get_data(as_text=True)
    assert resp.get_json()["job_id"]
    assert len(queue.job_ids) == 1


def test_analyze_record_failure_still_returns_result(client, monkeypatch):
    conn = fakeredis.FakeStrictRedis()
    monkeypatch.setattr("app.api.cart_recovery.get_redis_connection", lambda: conn)
    monkeypatch.setattr(
        "app.api.cart_recovery.run_cart_recovery", lambda cart, on_progress=None: _fake_insight()
    )

    def boom(*a, **k):
        raise RedisError("redis blip after pipeline")

    monkeypatch.setattr("app.api.cart_recovery.record", boom)
    resp = client.post(
        "/api/cart-recovery/analyze", json=VALID_PAYLOAD, headers={"Idempotency-Key": "idem-rec-2"}
    )
    # The ~8-17 min paid pipeline ran; a failed cache write must not 500 it away.
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["data"]["predicted_reason"] == "Shipping cost shock"
