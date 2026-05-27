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
)


def test_cart_recovery_analyze_emits_no_pii(client, caplog):
    # The handler reads these 7 attributes off the insight object
    # (see backend/app/api/cart_recovery.py:137-143). A SimpleNamespace
    # avoids importing the real cart_recovery package, which would pull
    # in the mirofish client SDK transitively via cart_recovery/__init__.py.
    fake_insight = SimpleNamespace(
        predicted_reason="stub reason",
        emotional_state="anxious",
        recommended_angle="discount-or-value",
        key_objections=[],
        email_prompt_context="",
        confidence=0.5,
        confidence_reasoning="",
    )

    def fake_analyze(cart, on_progress=None):
        # Exercise the on_progress callback so the INFO log line is captured.
        if on_progress is not None:
            on_progress("preparing", "stub progress message")
        return fake_insight

    fake_engine = MagicMock()
    fake_engine.analyze_abandonment.side_effect = fake_analyze

    with patch("app.api.cart_recovery._get_engine", return_value=fake_engine):
        with caplog.at_level(logging.DEBUG):
            resp = client.post(
                "/api/cart-recovery/analyze",
                json={
                    "customer_id": "cust_test_pii",
                    "customer_name": "Test PII Customer",
                    "email": "pii-test@example.com",
                    "checkout_token": "tok_test_checkout",
                    "location": "London, UK",
                    "cart_items": [{"product": "x", "price": 1.0, "quantity": 1}],
                    "cart_total": 1.0,
                },
            )

    assert resp.status_code == 200, resp.get_data(as_text=True)

    full_log = "\n".join(r.getMessage() for r in caplog.records)
    for token in PII_TOKENS:
        assert token not in full_log, (
            f"PII token leaked into logs: {token!r}\n--- full log ---\n{full_log}"
        )

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
