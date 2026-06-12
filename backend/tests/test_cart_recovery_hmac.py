"""Issue #11 — HMAC-SHA256 body signing on the paid cart-recovery POSTs.

The engine signs f"{timestamp}.{rawbody}" with the shared
WAKARU_INTERNAL_SECRET and sends X-Wakaru-Signature (hex) +
X-Wakaru-Timestamp (unix seconds at SEND time — never the cart-event time,
which is minutes-to-hours stale by design). The blueprint-level
verify_internal_hmac rejects missing/wrong signatures and timestamps outside
the ±5-minute window, POST-only, and runs after the app-level #10 key guard.

These tests build a plain (non-signing) client from create_app() so each case
controls its own headers; the suite-wide auto-signing client lives in
conftest.SigningFlaskClient. Synthetic identifiers use RFC 2606 (example.com).
"""
import hashlib
import hmac
import json
import time

import pytest

from app import create_app
from app.config import BANNED_WAKARU_INTERNAL_SECRET_DEFAULT
from tests.conftest import TEST_WAKARU_API_KEY, TEST_WAKARU_INTERNAL_SECRET

VALID_PAYLOAD = {
    "customer_id": "cust_test",
    "email": "shopper@example.com",
    "cart_items": [{"product": "x", "price": 1.0, "quantity": 1}],
    "cart_total": 1.0,
}

PAID_ENDPOINTS = ["/api/cart-recovery/jobs", "/api/cart-recovery/analyze"]


def _sign(body: bytes, ts=None, secret=TEST_WAKARU_INTERNAL_SECRET):
    """Headers the engine would send for `body` (default: fresh send time)."""
    ts = str(int(time.time()) if ts is None else int(ts))
    sig = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return {"X-Wakaru-Timestamp": ts, "X-Wakaru-Signature": sig}


@pytest.fixture
def plain_client():
    """Client that passes #10 auth but does NOT auto-sign (no
    SigningFlaskClient), so tests control the #11 headers explicitly."""
    app = create_app()
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
    c = app.test_client()
    c.environ_base["HTTP_X_API_KEY"] = TEST_WAKARU_API_KEY
    return c


# --------------------------------------------------------------------------
# Rejections
# --------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", PAID_ENDPOINTS)
def test_missing_signature_rejected(plain_client, endpoint):
    resp = plain_client.post(endpoint, json=VALID_PAYLOAD)
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "missing_signature"


def test_wrong_signature_rejected(plain_client):
    # Valid headers for DIFFERENT bytes: body substitution must not verify.
    body = json.dumps(VALID_PAYLOAD).encode()
    headers = _sign(b'{"customer_id":"tampered"}')
    resp = plain_client.post(
        "/api/cart-recovery/jobs", data=body,
        content_type="application/json", headers=headers,
    )
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "invalid_signature"


def test_expired_timestamp_rejected(plain_client):
    # A correctly signed but old request (captured + replayed) must die at the
    # window check even though the signature itself verifies.
    body = json.dumps(VALID_PAYLOAD).encode()
    headers = _sign(body, ts=int(time.time()) - 400)
    resp = plain_client.post(
        "/api/cart-recovery/jobs", data=body,
        content_type="application/json", headers=headers,
    )
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "expired_timestamp"


def test_future_timestamp_rejected(plain_client):
    # The window is symmetric: a far-future timestamp (hostile or badly skewed
    # clock) is as invalid as a stale one.
    body = json.dumps(VALID_PAYLOAD).encode()
    headers = _sign(body, ts=int(time.time()) + 400)
    resp = plain_client.post(
        "/api/cart-recovery/jobs", data=body,
        content_type="application/json", headers=headers,
    )
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "expired_timestamp"


def test_non_integer_timestamp_rejected(plain_client):
    resp = plain_client.post(
        "/api/cart-recovery/jobs", json=VALID_PAYLOAD,
        headers={"X-Wakaru-Timestamp": "not-a-number", "X-Wakaru-Signature": "ab" * 32},
    )
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "invalid_timestamp"


# --------------------------------------------------------------------------
# Pass-through
# --------------------------------------------------------------------------

def test_valid_signature_reaches_handler_and_get_json_still_works(plain_client):
    # A signed body missing "email" must get the handler's own 400 — proof the
    # request passed the HMAC gate AND that the gate's get_data() read did not
    # consume the body before the handler's get_json() (Flask caches it).
    body = json.dumps({"customer_id": "cust_test"}).encode()
    resp = plain_client.post(
        "/api/cart-recovery/jobs", data=body,
        content_type="application/json", headers=_sign(body),
    )
    assert resp.status_code == 400
    assert "email" in resp.get_json()["error"]


def test_poll_get_not_subject_to_hmac(plain_client):
    # The poll GET has no body to bind; it must never 401 for lack of a
    # signature (X-API-Key #10 already authenticated it).
    resp = plain_client.get("/api/cart-recovery/jobs/any-id")
    assert resp.status_code != 401


def test_api_key_guard_fires_before_hmac():
    # App-level #10 runs before the blueprint-level #11 hook: with no X-API-Key
    # at all, the response is the key guard's 401, not missing_signature.
    app = create_app()
    app.config["TESTING"] = True
    resp = app.test_client().post("/api/cart-recovery/jobs", json=VALID_PAYLOAD)
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


# --------------------------------------------------------------------------
# Fail-closed configuration
# --------------------------------------------------------------------------

def test_missing_secret_at_request_time_returns_503(plain_client, monkeypatch):
    # validate() blocks boot without the secret; this pins the request-time
    # defense in depth for any path that bypasses the boot gate.
    monkeypatch.delenv("WAKARU_INTERNAL_SECRET")
    body = json.dumps(VALID_PAYLOAD).encode()
    resp = plain_client.post(
        "/api/cart-recovery/jobs", data=body,
        content_type="application/json", headers=_sign(body),
    )
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "server_auth_not_configured"


def test_create_app_fails_closed_without_internal_secret(monkeypatch):
    monkeypatch.delenv("WAKARU_INTERNAL_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="WAKARU_INTERNAL_SECRET"):
        create_app()


def test_create_app_rejects_placeholder_internal_secret(monkeypatch):
    # The .env.example placeholder is public; booting with it would let anyone
    # forge signatures. Reject at boot like the #6/#10 literals.
    monkeypatch.setenv("WAKARU_INTERNAL_SECRET", BANNED_WAKARU_INTERNAL_SECRET_DEFAULT)
    with pytest.raises(RuntimeError, match="WAKARU_INTERNAL_SECRET"):
        create_app()


def test_create_app_rejects_whitespace_internal_secret(monkeypatch):
    monkeypatch.setenv("WAKARU_INTERNAL_SECRET", "   ")
    with pytest.raises(RuntimeError, match="WAKARU_INTERNAL_SECRET"):
        create_app()


# --------------------------------------------------------------------------
# Cross-repo contract
# --------------------------------------------------------------------------

def test_signature_cross_repo_vector():
    # The same fixed vector is pinned in the engine's
    # services/mirofish_hmac_test.go (TestWakaruSignature_CrossRepoVector). If
    # either side changes the signed string ("<timestamp>.<body>"), the key
    # derivation, or the hex encoding, one of the two suites goes red before
    # prod does.
    headers = _sign(
        b'{"customer_id":"c1"}', ts=1700000000, secret="contract-test-secret"
    )
    assert headers["X-Wakaru-Signature"] == (
        "85e86a9154397e23b9d3be5a059982f527d811da061e42ea082ae7471ae0c49d"
    )
