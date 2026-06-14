"""Unit tests for the reason_category classifier (issue #3).

Covers the Inkwell M2 contract: EmailPromptBuilder._classify_reason_category maps
the free-form predicted reason + simulation report to exactly one of the 7
REASON_CATEGORIES, always — never null, never out of enum, and "unknown" when the
report carries no usable signal.

The classifier is a pure, deterministic keyword scorer (no LLM, no network), so
these tests need neither the Flask app nor Redis — just the cart_recovery package
(the backend conftest puts the repo root on sys.path).
"""
from __future__ import annotations

import pytest

from cart_recovery.email_prompt_builder import REASON_CATEGORIES, EmailPromptBuilder


# One clear-cut simulation report per enum value. The text uses the same vocab a
# real ReportAgent markdown would, so the category stays consistent with the
# free-form predicted_reason a human would read.
_CLEAR_CUT = [
    (
        "shipping_cost",
        "Shipping cost shock at checkout",
        "The customer balked at the high shipping cost of $12.99 shown at "
        "checkout. No free shipping threshold was offered earlier in the journey.",
    ),
    (
        "price_sensitivity",
        "Product felt too expensive",
        "The product itself was simply too expensive for this shopper's budget; "
        "they could not afford it and went looking for something cheaper.",
    ),
    (
        "sizing_doubt",
        "Unsure about the fit",
        "The shopper was uncertain about the size and fit, worried the item would "
        "be too small and unsure whether it runs true to size.",
    ),
    (
        "payment_friction",
        "Payment step failed",
        "The payment failed at checkout — the customer's card was declined and the "
        "payment gateway returned a checkout error.",
    ),
    (
        "just_browsing",
        "Not ready to buy",
        "The shopper was just browsing and comparing alternatives, window shopping "
        "for a gift rather than intending to purchase today.",
    ),
    (
        "out_of_stock_concern",
        "Worried about availability",
        "The customer worried the item was low stock and nearly sold out, and "
        "might be out of stock before they could complete the order.",
    ),
]


@pytest.mark.parametrize("expected, reason, report", _CLEAR_CUT)
def test_classify_clear_cut_reports(expected, reason, report):
    """Each clear-cut report classifies to its category."""
    builder = EmailPromptBuilder()
    assert builder._classify_reason_category(report, reason) == expected


def test_classify_signal_free_report_is_unknown():
    """A report with no category signal resolves to 'unknown', not a guess."""
    builder = EmailPromptBuilder()
    report = (
        "The customer viewed the homepage and a collection page, then left the "
        "site. The simulation could not converge on a single driver."
    )
    assert builder._classify_reason_category(report, "Reason undetermined") == "unknown"


def test_classify_empty_inputs_is_unknown():
    """Empty report + reason is a no-signal case → 'unknown' (never null/empty)."""
    builder = EmailPromptBuilder()
    assert builder._classify_reason_category("", "") == "unknown"


@pytest.mark.parametrize("expected, reason, report", _CLEAR_CUT)
def test_classify_always_returns_valid_enum(expected, reason, report):
    """Every classification is a member of the 7-value contract enum."""
    builder = EmailPromptBuilder()
    assert builder._classify_reason_category(report, reason) in REASON_CATEGORIES


def test_reason_categories_is_the_seven_contract_values():
    """Pin the exact enum set Inkwell's planner is keyed on (no drift)."""
    assert set(REASON_CATEGORIES) == {
        "shipping_cost",
        "price_sensitivity",
        "sizing_doubt",
        "payment_friction",
        "just_browsing",
        "out_of_stock_concern",
        "unknown",
    }


def test_shipping_signal_beats_generic_price_on_tie():
    """A shipping-specific cost complaint classifies as shipping_cost, not
    price_sensitivity — the spec distinguishes balking at shipping from balking
    at the product price."""
    builder = EmailPromptBuilder()
    report = "The shopper abandoned over the shipping cost, not the item price."
    assert builder._classify_reason_category(report, "") == "shipping_cost"
