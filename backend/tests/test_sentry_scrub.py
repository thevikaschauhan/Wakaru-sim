"""Issue #17 — extend Sentry PII scrubbing to request.data (recursively) and
breadcrumbs.

Pure-function tests over the before_send (`_scrub_pii`) and before_breadcrumb
(`_scrub_breadcrumb`) hooks. Synthetic identifiers only (RFC 2606 example.com).
"""
from app import _redact_pii, _scrub_breadcrumb, _scrub_pii


def test_scrub_pii_redacts_top_level_and_nested():
    event = {
        "request": {
            "data": {
                "email": "leak@example.com",
                "customer_name": "Jane Doe",
                "cart_total": 49.99,
                "nested": {"customer_id": "cust_x", "sku": "ABC"},
                "browsing_history": ["home", "checkout"],
            }
        }
    }
    data = _scrub_pii(event, None)["request"]["data"]
    assert data["email"] == "[redacted]"
    assert data["customer_name"] == "[redacted]"
    assert data["cart_total"] == 49.99  # non-PII survives
    assert data["nested"]["customer_id"] == "[redacted]"  # nested PII redacted
    assert data["nested"]["sku"] == "ABC"
    assert data["browsing_history"] == "[redacted]"  # PII-keyed list redacted whole


def test_scrub_pii_redacts_pii_inside_list_of_dicts():
    event = {"request": {"data": {"items": [{"email": "a@example.com", "qty": 2}]}}}
    item = _scrub_pii(event, None)["request"]["data"]["items"][0]
    assert item["email"] == "[redacted]"
    assert item["qty"] == 2


def test_scrub_pii_strips_authorization_keeps_other_headers():
    event = {
        "request": {
            "headers": {"Authorization": "Bearer secret", "Content-Type": "application/json"}
        }
    }
    headers = _scrub_pii(event, None)["request"]["headers"]
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"


def test_scrub_pii_noop_without_request():
    event = {"level": "error", "message": "boom"}
    assert _scrub_pii(event, None) == {"level": "error", "message": "boom"}


def test_scrub_breadcrumb_redacts_data():
    crumb = {"category": "http", "data": {"email": "leak@example.com", "status_code": 500}}
    out = _scrub_breadcrumb(crumb, None)
    assert out["data"]["email"] == "[redacted]"
    assert out["data"]["status_code"] == 500


def test_scrub_breadcrumb_noop_without_data():
    crumb = {"category": "log", "message": "hello"}
    assert _scrub_breadcrumb(crumb, None) == {"category": "log", "message": "hello"}


def test_redact_pii_passthrough_non_container():
    assert _redact_pii("plain") == "plain"
    assert _redact_pii(42) == 42
    assert _redact_pii(None) is None


def test_redact_pii_caps_recursion_depth_without_raising():
    # A hostile deeply-nested payload must be redacted at the depth cap rather
    # than blowing the Python stack inside the Sentry before_send hook.
    deep = cur = {}
    for _ in range(2000):
        cur["next"] = {}
        cur = cur["next"]
    out = _redact_pii(deep)  # must not raise RecursionError
    assert "[redacted:depth]" in repr(out)


def test_redact_pii_string_passthrough_is_intentional():
    # request.data may be a raw string (form-encoded) if body capture is ever
    # re-enabled; redaction is by dict key, so strings pass through. Safe today
    # because max_request_body_size="never". This documents the known gap.
    assert _redact_pii("email=user@example.com") == "email=user@example.com"
