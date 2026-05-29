"""Issue #16 — OWASP baseline security headers on every response.

AC: `curl -I` against any endpoint returns the four headers; HSTS only in
production. The after_request hook reads os.environ["ENVIRONMENT"] at request
time, so monkeypatching it before the request is sufficient.
"""


def test_baseline_security_headers_present(client):
    resp = client.get("/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_hsts_absent_outside_production(client, monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    resp = client.get("/health")
    assert "Strict-Transport-Security" not in resp.headers


def test_hsts_present_in_production(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    resp = client.get("/health")
    assert resp.headers["Strict-Transport-Security"] == (
        "max-age=63072000; includeSubDomains"
    )
