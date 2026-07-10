"""
Issue #20 — async cart-recovery job queue (Redis + RQ).

Covers the new bridge endpoints (POST /jobs, GET /jobs/<id>) and the RQ job
body, using an in-memory fakeredis. Execution tests use RQ's synchronous mode
(``is_async=False``): ``enqueue()`` runs the job inline, exercising the real RQ
status / result / exc_info machinery + ``get_current_job`` while sidestepping the
fakeredis<->rq ``Worker`` ``client_list``/``addr`` incompatibility. The
synchronous /analyze path is unchanged and stays covered by
test_cart_recovery_pii.py.

Synthetic identifiers use RFC 2606 (`example.com`) so test data cannot collide
with any real customer record.
"""
import logging
from dataclasses import asdict
from types import SimpleNamespace

import fakeredis
import pytest
from rq import Queue
from rq.job import Job

from app.services.cart_recovery_jobs import run_analysis_job
from app.services.job_queue import (
    ANALYZE_QUEUE_NAME,
    DEFAULT_ANALYZE_JOB_TIMEOUT,
    MIN_ANALYZE_JOB_TIMEOUT,
    analyze_job_timeout,
    get_redis_connection,
)
from cart_recovery.shopify_formatter import ShopifyCartData

RESULT_KEYS = {
    "predicted_reason",
    "reason_category",
    "emotional_state",
    "recommended_angle",
    "key_objections",
    "email_prompt_context",
    "confidence",
    "confidence_reasoning",
}

VALID_PAYLOAD = {
    "customer_id": "cust_test",
    "email": "shopper@example.com",
    "cart_items": [{"product": "x", "price": 1.0, "quantity": 1}],
    "cart_total": 1.0,
}

PII_TOKENS = (
    "pii-test@example.com",
    "Test PII Customer",
    "cust_test_pii",
    "tok_test_checkout",
    "London, UK",
    "stripe_test_pii",
    "pii-browsing-history-token",
)

PII_PAYLOAD = {
    "customer_id": "cust_test_pii",
    "customer_name": "Test PII Customer",
    "email": "pii-test@example.com",
    "checkout_token": "tok_test_checkout",
    "location": "London, UK",
    "payment_gateway_attempted": "stripe_test_pii",
    "browsing_history": ["pii-browsing-history-token"],
    "cart_items": [{"product": "x", "price": 1.0, "quantity": 1}],
    "cart_total": 1.0,
}


def _stub_success(cart, on_progress=None):
    # Emit a PII-free progress tick (mirrors a real workflow stage) so the
    # job.meta progress path is exercised.
    if on_progress is not None:
        on_progress("graph_completed", {"project_id": "proj_test", "graph_id": "g_test"})
    return SimpleNamespace(
        predicted_reason="stub reason",
        reason_category="shipping_cost",
        emotional_state="anxious",
        recommended_angle="discount-or-value",
        key_objections=[],
        email_prompt_context="",
        confidence=0.5,
        confidence_reasoning="heuristic",
    )


def _assert_no_pii(text):
    for token in PII_TOKENS:
        assert token not in text, f"PII token leaked: {token!r}\n--- text ---\n{text}"


def _wire(monkeypatch, queue, connection):
    monkeypatch.setattr(
        "app.api.cart_recovery.get_analyze_queue", lambda connection=None: queue
    )
    monkeypatch.setattr(
        "app.api.cart_recovery.get_redis_connection", lambda: connection
    )


@pytest.fixture
def patched_queue(monkeypatch):
    """is_async=True queue (jobs stay queued) — for enqueue / poll-shape tests."""
    connection = fakeredis.FakeStrictRedis()
    queue = Queue(ANALYZE_QUEUE_NAME, connection=connection)
    _wire(monkeypatch, queue, connection)
    return queue


@pytest.fixture
def sync_queue(monkeypatch):
    """is_async=False queue — enqueue() runs the job inline so POST /jobs
    executes run_analysis_job and GET sees the terminal state."""
    connection = fakeredis.FakeStrictRedis()
    queue = Queue(ANALYZE_QUEUE_NAME, connection=connection, is_async=False)
    _wire(monkeypatch, queue, connection)
    return queue


# --------------------------------------------------------------------------
# POST /jobs — enqueue (jobs stay queued)
# --------------------------------------------------------------------------

def test_enqueue_returns_202_and_job_id(client, patched_queue):
    resp = client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD)
    assert resp.status_code == 202, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["success"] is True
    job_id = data["job_id"]
    assert data["status_url"] == f"/api/cart-recovery/jobs/{job_id}"
    assert job_id in patched_queue.job_ids


def test_enqueue_sets_timeout_and_ttls(client, patched_queue):
    resp = client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD)
    job = Job.fetch(resp.get_json()["job_id"], connection=patched_queue.connection)
    assert job.timeout == 5400
    assert job.result_ttl == 86400
    assert job.failure_ttl == 86400


def test_enqueue_503_when_redis_unconfigured(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.cart_recovery.get_analyze_queue", lambda connection=None: None
    )
    resp = client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD)
    assert resp.status_code == 503


def test_enqueue_invalid_json_400(client, patched_queue):
    resp = client.post(
        "/api/cart-recovery/jobs", data="not json", content_type="application/json"
    )
    assert resp.status_code == 400


def test_enqueue_missing_fields_400(client, patched_queue):
    resp = client.post("/api/cart-recovery/jobs", json={"customer_id": "c"})
    assert resp.status_code == 400


def test_enqueue_invalid_payload_does_not_echo_pii(client, patched_queue, caplog):
    bad = {**PII_PAYLOAD, "cart_total": "pii-test@example.com"}
    with caplog.at_level(logging.DEBUG, logger="mirofish.cart_recovery"):
        resp = client.post("/api/cart-recovery/jobs", json=bad)
    assert resp.status_code == 400
    _assert_no_pii("\n".join(r.getMessage() for r in caplog.records))


# --------------------------------------------------------------------------
# GET /jobs/<id> — poll
# --------------------------------------------------------------------------

def test_get_queued_job(client, patched_queue):
    job_id = client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD).get_json()["job_id"]
    resp = client.get(f"/api/cart-recovery/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["job_id"] == job_id
    assert data["status"] == "queued"
    assert "result" not in data
    assert "error" not in data


def test_get_unknown_job_404(client, patched_queue):
    resp = client.get("/api/cart-recovery/jobs/does-not-exist")
    assert resp.status_code == 404


def test_get_503_when_redis_unconfigured(client, monkeypatch):
    monkeypatch.setattr("app.api.cart_recovery.get_redis_connection", lambda: None)
    resp = client.get("/api/cart-recovery/jobs/whatever")
    assert resp.status_code == 503


def test_finished_job_returns_result_and_progress(client, sync_queue, monkeypatch):
    monkeypatch.setattr(
        "app.services.cart_recovery_jobs.run_cart_recovery", _stub_success
    )
    job_id = client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD).get_json()["job_id"]

    resp = client.get(f"/api/cart-recovery/jobs/{job_id}")
    data = resp.get_json()
    assert data["status"] == "finished"
    assert set(data["result"].keys()) == RESULT_KEYS
    assert data["result"]["predicted_reason"] == "stub reason"
    assert data["result"]["reason_category"] == "shipping_cost"
    assert data["result"]["confidence_reasoning"] == "heuristic"
    # Progress was persisted to Redis (survives a web-worker restart).
    assert data["progress"].get("stage") == "graph_completed"


def test_failed_job_is_pii_safe(client, sync_queue, monkeypatch):
    def boom(cart, on_progress=None):
        # The original message embeds PII — it must NOT survive into the job record.
        raise RuntimeError("boom for pii-test@example.com / cust_test_pii")

    monkeypatch.setattr("app.services.cart_recovery_jobs.run_cart_recovery", boom)
    job_id = client.post("/api/cart-recovery/jobs", json=PII_PAYLOAD).get_json()["job_id"]

    resp = client.get(f"/api/cart-recovery/jobs/{job_id}")
    data = resp.get_json()
    assert data["status"] == "failed"
    assert data["error"] == "Analysis failed (RuntimeError)"
    _assert_no_pii(resp.get_data(as_text=True))

    # The raw RQ exc_info must not carry the original (PII-bearing) message.
    job = Job.fetch(job_id, connection=sync_queue.connection)
    assert "boom for" not in (job.exc_info or "")
    _assert_no_pii(job.exc_info or "")


def test_failed_job_log_record_carries_request_id_job_id_merchant_id(
    client, sync_queue, monkeypatch, caplog
):
    """Issue #26: the worker has no Flask g, so request_id/job_id/merchant_id
    must reach the log record via `extra=` (JsonFormatter's own docstring
    promise) — otherwise the one log line an operator would query after an
    async analysis failure carries none of them as structured fields.

    Asserts on the LogRecord's extra attributes directly via caplog, not on
    rendered stdout text: setup_logger() caches each named logger's handler
    for the lifetime of the process (first-configured-wins), so a shared
    logger like mirofish.cart_recovery can end up bound to a stdout
    reference from a different test's capsys context — caplog sidesteps this
    entirely by capturing LogRecord objects via propagation."""

    def boom(cart, on_progress=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.cart_recovery_jobs.run_cart_recovery", boom)
    with caplog.at_level(logging.ERROR, logger="mirofish.cart_recovery"):
        job_id = client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD).get_json()["job_id"]

    records = [r for r in caplog.records if r.name == "mirofish.cart_recovery"]
    assert records, "expected a mirofish.cart_recovery log record"
    record = records[-1]
    assert record.job_id == job_id
    assert record.request_id == job_id[:8]
    assert hasattr(record, "merchant_id")


def test_job_meta_progress_is_pii_free(client, sync_queue, monkeypatch):
    monkeypatch.setattr(
        "app.services.cart_recovery_jobs.run_cart_recovery", _stub_success
    )
    job_id = client.post("/api/cart-recovery/jobs", json=PII_PAYLOAD).get_json()["job_id"]
    job = Job.fetch(job_id, connection=sync_queue.connection)
    _assert_no_pii(repr(job.meta))


# --------------------------------------------------------------------------
# run_analysis_job — direct (no worker / no job context)
# --------------------------------------------------------------------------

def test_run_analysis_job_returns_result_dict(monkeypatch):
    monkeypatch.setattr(
        "app.services.cart_recovery_jobs.run_cart_recovery", _stub_success
    )
    cart_dict = asdict(
        ShopifyCartData(
            customer_id="c",
            customer_name="n",
            email="e@example.com",
            cart_items=[],
            cart_total=0.0,
        )
    )
    result = run_analysis_job(cart_dict)
    assert set(result.keys()) == RESULT_KEYS


def test_run_analysis_job_rebuilds_cart_faithfully(monkeypatch):
    captured = {}

    def capture(cart, on_progress=None):
        captured["cart"] = cart
        return _stub_success(cart, on_progress)

    monkeypatch.setattr("app.services.cart_recovery_jobs.run_cart_recovery", capture)
    original = ShopifyCartData(
        customer_id="c",
        customer_name="n",
        email="e@example.com",
        cart_items=[{"product": "x", "price": 1.0, "quantity": 2}],
        cart_total=2.0,
        device_type="mobile",
    )
    run_analysis_job(asdict(original))
    # The worker-side rebuild must equal what the web tier serialized, incl. the
    # device/device_type __post_init__ alias.
    assert captured["cart"] == original


# --------------------------------------------------------------------------
# Review fixes — PII in job.description, job_timeout floor, malformed REDIS_URL
# --------------------------------------------------------------------------

def test_enqueue_description_is_pii_free(client, patched_queue):
    """RQ's default job description renders the call args (the cart dict, which
    carries PII) into a string stored in Redis + logged at dequeue. The enqueue
    must pin a PII-free description instead."""
    job_id = client.post("/api/cart-recovery/jobs", json=PII_PAYLOAD).get_json()["job_id"]
    job = Job.fetch(job_id, connection=patched_queue.connection)
    assert job.description == "cart-recovery analysis"
    _assert_no_pii(job.description or "")


def test_analyze_job_timeout_floor(monkeypatch):
    monkeypatch.delenv("ANALYZE_JOB_TIMEOUT", raising=False)
    assert analyze_job_timeout() == DEFAULT_ANALYZE_JOB_TIMEOUT
    # Below the floor -> unsafe -> safe default (would SIGKILL worker mid-pipeline).
    monkeypatch.setenv("ANALYZE_JOB_TIMEOUT", "60")
    assert analyze_job_timeout() == DEFAULT_ANALYZE_JOB_TIMEOUT
    # The bare poll ceiling (3480s) leaves no room for pre/post stages -> rejected.
    monkeypatch.setenv("ANALYZE_JOB_TIMEOUT", "3480")
    assert analyze_job_timeout() == DEFAULT_ANALYZE_JOB_TIMEOUT
    # Exact boundary: MIN is safe and honoured; MIN-1 falls back to the default.
    monkeypatch.setenv("ANALYZE_JOB_TIMEOUT", str(MIN_ANALYZE_JOB_TIMEOUT))
    assert analyze_job_timeout() == MIN_ANALYZE_JOB_TIMEOUT
    monkeypatch.setenv("ANALYZE_JOB_TIMEOUT", str(MIN_ANALYZE_JOB_TIMEOUT - 1))
    assert analyze_job_timeout() == DEFAULT_ANALYZE_JOB_TIMEOUT
    # Non-integer -> safe default.
    monkeypatch.setenv("ANALYZE_JOB_TIMEOUT", "not-a-number")
    assert analyze_job_timeout() == DEFAULT_ANALYZE_JOB_TIMEOUT
    # A safe higher value is honoured verbatim.
    monkeypatch.setenv("ANALYZE_JOB_TIMEOUT", "7200")
    assert analyze_job_timeout() == 7200


def test_get_redis_connection_malformed_url_returns_none(monkeypatch):
    """A bad scheme makes redis.from_url raise ValueError (not RedisError); it
    must surface as None (-> 503), not an unhandled 500."""
    monkeypatch.setenv("REDIS_URL", "not-a-redis-url")
    assert get_redis_connection() is None
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert get_redis_connection() is None
