"""Issue #14 — global error handler + the traceback/str(e) info-leak sweep.

Security baseline pinned here:
- an unhandled server-side exception returns an opaque 500 (never a stack trace
  or raw exception string),
- the full traceback still reaches the server log for triage,
- intentional HTTPExceptions pass through unchanged (the #10 auth 401, the #12
  rate-limit 429 + Retry-After) and are not masked as internal_error,
- a static guard pins that no API blueprint reintroduces a "traceback" field.
"""
import logging
from pathlib import Path

import pytest

from app import create_app
from tests.conftest import TEST_MERCHANT_ID, TEST_WAKARU_API_KEY, SigningFlaskClient

# The raising view embeds a recognisable string + a fake internal path so the
# leak tests can assert that neither str(e) nor a path reaches the response.
_LEAK_MARKER = "leaky-detail-/opt/app/secret/module.py"

VALID_PAYLOAD = {
    "customer_id": "cust_test",
    "cart_items": [{"product": "x", "price": 1.0, "quantity": 1}],
    "cart_total": 1.0,
}


def _raise_view():
    raise RuntimeError(_LEAK_MARKER)


@pytest.fixture
def boom_app():
    """An app whose @errorhandler(Exception) actually runs.

    TESTING=True sets PROPAGATE_EXCEPTIONS=True, which would re-raise into the
    test instead of invoking the handler (prep D4), so we force it off. A
    throwaway /_boom route raises a non-HTTP exception; it is not under /api/ so
    the #10 auth guard does not gate it. mirofish.propagate is flipped on so
    caplog can observe the server-side log line (production keeps it False)."""
    app = create_app()
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.config["RATELIMIT_ENABLED"] = False
    app.add_url_rule("/_boom", "_boom", _raise_view)

    mirofish_logger = logging.getLogger("mirofish")
    original_propagate = mirofish_logger.propagate
    mirofish_logger.propagate = True
    yield app
    mirofish_logger.propagate = original_propagate


def test_unhandled_exception_returns_opaque_500(boom_app):
    resp = boom_app.test_client().get("/_boom")
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["error"] == "internal_error"
    assert body["request_id"]  # present and non-empty
    assert "traceback" not in body


def test_unhandled_exception_does_not_leak_detail(boom_app):
    # Neither the exception string nor any internal path may appear in the body.
    text = boom_app.test_client().get("/_boom").get_data(as_text=True)
    assert _LEAK_MARKER not in text
    assert "RuntimeError" not in text
    assert "Traceback" not in text


def test_unhandled_exception_logs_traceback_server_side(boom_app, caplog):
    with caplog.at_level(logging.DEBUG, logger="mirofish"):
        resp = boom_app.test_client().get("/_boom")
    request_id = resp.get_json()["request_id"]
    # Full traceback is in the server log (DEBUG) for triage...
    assert "Traceback (most recent call last)" in caplog.text
    assert _LEAK_MARKER in caplog.text
    # ...and the log line carries the same id returned to the caller.
    assert f"[{request_id}]" in caplog.text


def test_security_headers_present_on_500(boom_app):
    # The #16 after_request headers must still land on an error-handler
    # response, not only on 2xx.
    resp = boom_app.test_client().get("/_boom")
    assert resp.status_code == 500
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_auth_401_not_masked_by_handler(app):
    # The #10 guard returns a 401 tuple (it does not raise), so the global
    # handler must not turn it into internal_error.
    resp = app.test_client().get("/api/cart-recovery/jobs/any-id")
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


def test_rate_limit_429_passes_through_with_retry_after(monkeypatch):
    # The limiter raises RateLimitExceeded (an HTTPException). The global handler
    # re-renders HTTPExceptions unchanged, so the 429 + Retry-After must survive
    # and never collapse into internal_error.
    monkeypatch.setenv("CART_RECOVERY_RATE_LIMIT_PER_MIN", "1")
    app = create_app()
    app.config["TESTING"] = True
    app.test_client_class = SigningFlaskClient  # pass #11 HMAC (body-signed POSTs)
    client = app.test_client()
    client.environ_base["HTTP_X_API_KEY"] = TEST_WAKARU_API_KEY
    client.environ_base["HTTP_X_MERCHANT_ID"] = TEST_MERCHANT_ID  # pass #24 merchant gate

    statuses = [
        client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD).status_code
        for _ in range(2)
    ]
    assert 429 in statuses, statuses
    resp = client.post("/api/cart-recovery/jobs", json=VALID_PAYLOAD)
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") is not None
    # The limiter's own 429 body (not our opaque 500) is rendered through.
    assert "internal_error" not in resp.get_data(as_text=True)


def test_no_api_handler_returns_traceback_field():
    # Static guard (acceptance): no API blueprint may echo a "traceback" field in
    # a response. Server-side logging of the traceback is fine; this scans for
    # the JSON key, which after the sweep appears nowhere in app/api/.
    api_dir = Path(__file__).resolve().parent.parent / "app" / "api"
    offenders = [
        p.name
        for p in api_dir.glob("*.py")
        if '"traceback"' in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"traceback field leaked in API responses: {offenders}"
