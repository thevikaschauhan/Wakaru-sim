"""
Issue #7 — assert that POST /api/cart-recovery/analyze emits no log line
containing any of the PII tokens listed in the issue's AC.

Synthetic identifiers use RFC 2606 (`example.com`) so the test data cannot
collide with any real customer record.
"""
import logging
import re
from types import SimpleNamespace
from unittest.mock import patch

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


def _assert_no_pii(caplog_records):
    full_log = "\n".join(r.getMessage() for r in caplog_records)
    for token in PII_TOKENS:
        assert token not in full_log, (
            f"PII token leaked into logs: {token!r}\n--- full log ---\n{full_log}"
        )


def test_cart_recovery_analyze_emits_no_pii(client, caplog):
    def fake_analyze(cart, on_progress=None):
        # Exercise the on_progress callback so the INFO log line is captured.
        if on_progress is not None:
            on_progress("preparing", "stub progress message")
        # SimpleNamespace avoids importing the real cart_recovery package,
        # which would transitively pull in the mirofish client SDK via
        # cart_recovery/__init__.py.
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

    with patch("app.api.cart_recovery.run_cart_recovery", side_effect=fake_analyze):
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
    assert re.match(r"\[[0-9a-f]{8} m=[0-9a-f-]{36}\] ", progress_lines[0]), (
        f"Expected an 8-hex request_id + merchant_id (#24) prefix on the "
        f"progress log, got: {progress_lines[0]!r}"
    )


def test_cart_recovery_exception_path_emits_no_pii(client, caplog):
    """When the engine raises, the 500-error log must include the request_id
    prefix and must not embed customer_id or other PII."""
    with patch("app.api.cart_recovery.run_cart_recovery", side_effect=RuntimeError("engine boom")):
        with caplog.at_level(logging.DEBUG, logger="mirofish.cart_recovery"):
            with caplog.at_level(logging.DEBUG, logger="mirofish.request"):
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
    assert re.match(r"\[[0-9a-f]{8} m=[0-9a-f-]{36}\] ", error_lines[0]), (
        f"Expected an 8-hex request_id + merchant_id (#24) prefix on the "
        f"error log, got: {error_lines[0]!r}"
    )


def test_cart_recovery_invalid_payload_does_not_echo_pii(client, caplog):
    """A caller mistakenly putting a PII string in a numeric field
    (here `cart_total`) raises ValueError inside ShopifyCartData construction.
    The 400-warning log must NOT echo the offending value into the record."""
    # Reuse the full PII payload so any future PII token added to
    # PII_TOKENS / PII_PAYLOAD is automatically exercised through this
    # path too. Override cart_total with a PII string to force the
    # ValueError path inside ShopifyCartData construction.
    bad_payload = {**PII_PAYLOAD, "cart_total": "pii-test@example.com"}

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
