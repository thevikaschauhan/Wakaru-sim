"""Issue #10 — X-API-Key auth guard + CORS allowlist on /api/*.

In production the engine is the only caller of /api/* (server→server). These
tests pin the security baseline:
- unauthenticated /api/* → 401
- wrong key → 401
- a valid key passes the guard and reaches the handler/validation
- /health stays open (it is not under /api/)
- CORS preflight is restricted to the ALLOWED_ORIGINS allowlist (no wildcard)
- the OPTIONS preflight itself is never 401'd by the guard
- WAKARU_API_KEY is fail-closed at boot via Config.validate()

The autouse `config_env` fixture sets WAKARU_API_KEY and the `client` fixture
injects it as a default header, so tests that want an *un*authenticated request
build their own client from the `app` fixture. Synthetic identifiers use
RFC 2606 (example.com).
"""
import pytest

from app import create_app

VALID_PAYLOAD = {
    "customer_id": "cust_test",
    "email": "shopper@example.com",
    "cart_items": [{"product": "x", "price": 1.0, "quantity": 1}],
    "cart_total": 1.0,
}


def test_unauthenticated_api_request_returns_401(app):
    resp = app.test_client().post("/api/cart-recovery/jobs", json=VALID_PAYLOAD)
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


def test_wrong_api_key_returns_401(app):
    resp = app.test_client().post(
        "/api/cart-recovery/jobs",
        json=VALID_PAYLOAD,
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_valid_api_key_passes_auth(client):
    # `client` injects the valid key. A non-JSON body proves the request reached
    # _build_cart_from_body (auth passed) instead of being 401'd at the guard.
    resp = client.post(
        "/api/cart-recovery/jobs", data="not json", content_type="application/json"
    )
    assert resp.status_code == 400  # auth passed; body validation rejected it


def test_health_endpoint_open_without_key(app):
    resp = app.test_client().get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_options_preflight_not_blocked_by_auth(app):
    # CORS preflight carries no X-API-Key; the guard must exempt OPTIONS so a
    # browser preflight is never 401'd.
    resp = app.test_client().open("/api/cart-recovery/jobs", method="OPTIONS")
    assert resp.status_code != 401


def test_cors_does_not_allow_arbitrary_origin(app):
    # With the locked allowlist, a random origin is not echoed and there is no
    # wildcard. (The engine calls server→server and needs no CORS at all.)
    resp = app.test_client().open(
        "/api/cart-recovery/jobs",
        method="OPTIONS",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    acao = resp.headers.get("Access-Control-Allow-Origin")
    assert acao != "https://evil.example"
    assert acao != "*"


def test_create_app_fails_closed_without_wakaru_api_key(monkeypatch):
    monkeypatch.delenv("WAKARU_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="WAKARU_API_KEY"):
        create_app()
