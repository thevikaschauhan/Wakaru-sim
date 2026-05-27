"""
Issue #7 — assert that POST /api/cart-recovery/analyze emits no log line
containing any of the PII tokens listed in the issue's AC.

Synthetic identifiers use RFC 2606 (`example.com`) so the test data cannot
collide with any real customer record.
"""
import logging
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


def _make_fake_insight():
    # The handler reads these 7 attributes off the insight object
    # (see backend/app/api/cart_recovery.py). SimpleNamespace avoids
    # importing the real cart_recovery package, which would pull in the
    # mirofish client SDK transitively via cart_recovery/__init__.py.
    return SimpleNamespace(
        predicted_reason="stub reason",
        emotional_state="anxious",
        recommended_angle="discount-or-value",
        key_objections=[],
        email_prompt_context="",
        confidence=0.5,
        confidence_reasoning="",
    )


def _assert_no_pii(caplog_records):
    full_log = "\n".join(r.getMessage() for r in caplog_records)
    for token in PII_TOKENS:
        assert token not in full_log, (
            f"PII token leaked into logs: {token!r}\n--- full log ---\n{full_log}"
        )


def test_cart_recovery_analyze_emits_no_pii(client, caplog):
    fake_engine = MagicMock()

    def fake_analyze(cart, on_progress=None):
        # Exercise the on_progress callback so the INFO log line is captured.
        if on_progress is not None:
            on_progress("preparing", "stub progress message")
        return _make_fake_insight()

    fake_engine.analyze_abandonment.side_effect = fake_analyze

    with patch("app.api.cart_recovery._get_engine", return_value=fake_engine):
        with caplog.at_level(logging.DEBUG, logger="mirofish.cart_recovery"):
            with caplog.at_level(logging.DEBUG, logger="mirofish.request"):
                resp = client.post("/api/cart-recovery/analyze", json=PII_PAYLOAD)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    _assert_no_pii(caplog.records)

    # Confirm the INFO progress line fired and carries the request-id prefix
    # so the test cannot silently pass via a broken mock or absent log path.
    progress_lines = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and "stub progress message" in r.getMessage()
    ]
    assert progress_lines, "Expected an INFO progress log line to be captured"
    assert re.match(r"\[[0-9a-f]{8}\] ", progress_lines[0]), (
        f"Expected an 8-hex request_id prefix on the progress log, "
        f"got: {progress_lines[0]!r}"
    )


def test_cart_recovery_exception_path_emits_no_pii(client, caplog):
    """When the engine raises, the 500-error log must include the request_id
    prefix and must not embed customer_id or other PII."""
    fake_engine = MagicMock()
    fake_engine.analyze_abandonment.side_effect = RuntimeError("engine boom")

    with patch("app.api.cart_recovery._get_engine", return_value=fake_engine):
        with caplog.at_level(logging.DEBUG, logger="mirofish.cart_recovery"):
            resp = client.post("/api/cart-recovery/analyze", json=PII_PAYLOAD)

    assert resp.status_code == 500
    _assert_no_pii(caplog.records)

    error_lines = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR
        and "Cart recovery analysis failed" in r.getMessage()
    ]
    assert error_lines, "Expected the ERROR log line to be captured"
    assert re.match(r"\[[0-9a-f]{8}\] ", error_lines[0]), (
        f"Expected an 8-hex request_id prefix on the error log, "
        f"got: {error_lines[0]!r}"
    )


def test_cart_recovery_invalid_payload_does_not_echo_pii(client, caplog):
    """A caller mistakenly putting a PII string in a numeric field
    (here `cart_total`) raises ValueError inside ShopifyCartData construction.
    The 400-warning log must NOT echo the offending value into the record."""
    bad_payload = {
        "customer_id": "cust_test_pii",
        "email": "pii-test@example.com",
        # PII value put in a numeric field on purpose:
        "cart_total": "pii-test@example.com",
        "cart_items": [{"product": "x", "price": 1.0, "quantity": 1}],
    }

    with caplog.at_level(logging.DEBUG, logger="mirofish.cart_recovery"):
        resp = client.post("/api/cart-recovery/analyze", json=bad_payload)

    assert resp.status_code == 400
    _assert_no_pii(caplog.records)

    warning_lines = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "Invalid cart data" in r.getMessage()
    ]
    assert warning_lines, "Expected the WARNING log line to be captured"
