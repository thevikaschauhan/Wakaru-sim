"""Issue #47 — unit tests for the structured-insight extraction in
``cart_recovery/email_prompt_builder.py``.

#47 replaces the fragile regex prose-scraping with an injected ``chat_json``
extractor (Fork A1 + B1): ``build(report, cart, *, chat_json=...)`` makes one
JSON LLM call for the fragile prose fields (``predicted_reason``,
``emotional_state``, ``key_objections``) while ``reason_category`` stays the
deterministic classifier and ``_choose_angle`` keeps its rotation logic
(Fork C). On ANY extraction failure the call falls back to the existing
heuristics (Fork D) — the paid ~8-17 min analysis must never be lost to a
provider blip.

These tests inject a fake ``chat_json`` (no network) and assert PROPERTIES:
the prose fields flow through, the staging objection-fragment bug cannot recur,
``reason_category`` is always in-enum, the angle business logic survives, and
every failure mode degrades to heuristics instead of raising.
"""
from __future__ import annotations

from cart_recovery.email_prompt_builder import (
    REASON_CATEGORIES,
    AbandonmentInsight,
    EmailPromptBuilder,
)
from cart_recovery.shopify_formatter import ShopifyCartData


def _make_cart(**overrides) -> ShopifyCartData:
    base = dict(
        customer_id="cust_x",
        customer_name="Shopper",
        email="shopper@example.com",
        cart_items=[{"product": "Widget", "price": 45.0, "quantity": 1}],
        cart_total=45.0,
    )
    base.update(overrides)
    return ShopifyCartData(**base)


class _FakeChatJSON:
    """A chat_json-style callable: records the messages it was called with and
    returns a canned dict, or raises ``boom`` to exercise the fail-safe path."""

    def __init__(self, result=None, boom: Exception | None = None):
        self.result = result  # returned verbatim (None included) unless boom is set
        self.boom = boom
        self.calls: list = []

    def __call__(self, messages, *args, **kwargs):
        self.calls.append(messages)
        if self.boom is not None:
            raise self.boom
        return self.result


# The real staging-run fragments the regex scraper produced (#47 issue body).
# The LLM path must never surface these.
_STAGING_FRAGMENTS = [
    "and Barriers to Conversion",
    "with the precision of a financial analyst:",
    "the $13 surprise that broke the trust frame**",
]

# A report whose prose would make the old `_extract_objections` regex scrape the
# staging fragments (headings/sentences after a trigger word).
_FRAGMENTY_REPORT = (
    "## Objections and Barriers to Conversion\n"
    "Analysing with the precision of a financial analyst:\n"
    "The concern: the $13 surprise that broke the trust frame**\n"
)


def test_no_extractor_uses_heuristics_unchanged():
    """Guard: with no chat_json injected, build() keeps the pre-#47 heuristic
    behaviour (backward compatible — other callers/tests are unaffected)."""
    builder = EmailPromptBuilder()
    report = "Primary reason: high shipping cost at checkout. The shopper balked."
    insight = builder.build(report, _make_cart())

    assert isinstance(insight, AbandonmentInsight)
    assert insight.predicted_reason == builder._extract_reason(report)
    assert insight.reason_category in REASON_CATEGORIES


def test_llm_extractor_populates_prose_fields():
    """When chat_json succeeds, its prose fields flow into the insight."""
    chat_json = _FakeChatJSON({
        "predicted_reason": "Shipping cost shock at checkout",
        "emotional_state": "price-sensitive",
        "key_objections": [
            "$12.99 shipping on a $45 order",
            "No free shipping threshold shown",
        ],
    })
    insight = EmailPromptBuilder().build(
        "Report body about shipping.", _make_cart(), chat_json=chat_json
    )

    assert chat_json.calls, "chat_json should have been invoked"
    assert insight.predicted_reason == "Shipping cost shock at checkout"
    assert insight.emotional_state == "price-sensitive"
    assert insight.key_objections == [
        "$12.99 shipping on a $45 order",
        "No free shipping threshold shown",
    ]


def test_llm_path_never_surfaces_objection_fragments():
    """#47 regression: even given a report whose prose the old regex would
    fragment-scrape, the LLM path yields the extractor's clean phrases and none
    of the staging heading/sentence fragments."""
    clean = ["$13 surprise shipping fee", "Trust concerns at checkout"]
    chat_json = _FakeChatJSON({
        "predicted_reason": "Unexpected shipping fee broke trust",
        "emotional_state": "trust-lacking",
        "key_objections": clean,
    })
    insight = EmailPromptBuilder().build(
        _FRAGMENTY_REPORT, _make_cart(), chat_json=chat_json
    )

    assert insight.key_objections == clean
    for frag in _STAGING_FRAGMENTS:
        assert frag not in insight.key_objections


def test_reason_category_classified_in_enum_on_llm_path():
    """reason_category stays the deterministic classifier (Fork C): it is derived
    from report+reason, always one of the 7 enum values."""
    report = (
        "The product itself was simply too expensive for this shopper's budget; "
        "they could not afford it and went looking for something cheaper."
    )
    chat_json = _FakeChatJSON({
        "predicted_reason": "Product felt too expensive",
        "emotional_state": "price-sensitive",
        "key_objections": ["Price above budget"],
    })
    insight = EmailPromptBuilder().build(report, _make_cart(), chat_json=chat_json)

    assert insight.reason_category == "price_sensitivity"
    assert insight.reason_category in REASON_CATEGORIES


def test_reason_category_unknown_when_signal_free_even_with_llm_reason():
    """A novel LLM reason over a signal-free report still coerces to 'unknown'
    (never an out-of-enum value reaching the Inkwell contract)."""
    chat_json = _FakeChatJSON({
        "predicted_reason": "A wholly novel undescribed reason",
        "emotional_state": "indecisive",
        "key_objections": [],
    })
    report = "The shopper viewed two pages and left without a clear driver."
    insight = EmailPromptBuilder().build(report, _make_cart(), chat_json=chat_json)

    assert insight.reason_category == "unknown"
    assert insight.reason_category in REASON_CATEGORIES


def test_extractor_exception_falls_back_to_heuristics():
    """Fork D: a raising extractor must NOT fail build(); it degrades to the
    heuristic extractors so the paid analysis result is preserved."""
    builder = EmailPromptBuilder()
    report = "Primary reason: high shipping cost at checkout. The shopper balked."
    chat_json = _FakeChatJSON(boom=RuntimeError("provider timeout"))

    insight = builder.build(report, _make_cart(), chat_json=chat_json)

    assert isinstance(insight, AbandonmentInsight)
    # identical to the no-LLM heuristic extraction of the same report
    assert insight.predicted_reason == builder._extract_reason(report)
    assert insight.reason_category in REASON_CATEGORIES


def test_extractor_missing_required_fields_falls_back():
    """An LLM result missing the gating prose fields degrades to heuristics."""
    builder = EmailPromptBuilder()
    report = "Primary reason: high shipping cost at checkout. The shopper balked."

    for bad in ({}, {"emotional_state": "anxious"}, {"predicted_reason": ""}):
        insight = builder.build(report, _make_cart(), chat_json=_FakeChatJSON(bad))
        assert insight.predicted_reason == builder._extract_reason(report)


def test_extractor_nondict_return_falls_back_to_heuristics():
    """A non-dict chat_json return (list/str/None/int) degrades to heuristics."""
    builder = EmailPromptBuilder()
    report = "Primary reason: high shipping cost at checkout. The shopper balked."

    for bad in (["a list"], "a string", None, 42):
        insight = builder.build(report, _make_cart(), chat_json=_FakeChatJSON(bad))
        assert insight.predicted_reason == builder._extract_reason(report)


def test_extractor_nonlist_objections_yield_empty_not_fragments():
    """When reason+emotion are valid but key_objections is malformed, accept the
    prose fields and use [] for objections — never reintroduce the regex
    fragments by falling back to the scraper for objections."""
    chat_json = _FakeChatJSON({
        "predicted_reason": "Shipping cost shock",
        "emotional_state": "price-sensitive",
        "key_objections": "not a list",
    })
    insight = EmailPromptBuilder().build(
        _FRAGMENTY_REPORT, _make_cart(), chat_json=chat_json
    )

    assert insight.predicted_reason == "Shipping cost shock"
    assert insight.key_objections == []


def test_objections_cleaned_deduped_capped():
    """Objection phrases are trimmed, de-duplicated, emptied-out, and capped at 5
    (mirrors the heuristic extractor's cap)."""
    chat_json = _FakeChatJSON({
        "predicted_reason": "Many concerns",
        "emotional_state": "anxious",
        "key_objections": [
            "  Shipping too high  ",
            "Shipping too high",      # dup after strip
            "",                        # empty -> dropped
            "Item too pricey",
            "Sizing unclear",
            "Returns policy unclear",
            "Delivery too slow",
            "Trust concerns",          # 6th distinct -> dropped by cap
        ],
    })
    insight = EmailPromptBuilder().build(
        "Report.", _make_cart(), chat_json=chat_json
    )

    assert insight.key_objections == [
        "Shipping too high",
        "Item too pricey",
        "Sizing unclear",
        "Returns policy unclear",
        "Delivery too slow",
    ]


def test_angle_rotation_preserved_on_llm_path():
    """Fork C: recommended_angle stays deterministic business logic. A prior
    failed angle is still rotated away from, even when the LLM supplies the
    prose fields."""
    report = "The shopper balked at the price; a discount might help."  # base -> discount-or-value
    cart = _make_cart(
        recovery_history=[{"angle": "discount-or-value", "outcome": "ignored"}]
    )
    chat_json = _FakeChatJSON({
        "predicted_reason": "Price too high",
        "emotional_state": "price-sensitive",
        "key_objections": ["Too expensive"],
    })
    insight = EmailPromptBuilder().build(report, cart, chat_json=chat_json)

    assert insight.recommended_angle != "discount-or-value"


def test_chat_json_called_with_messages_list():
    """The injected callable is invoked with an OpenAI-style messages list."""
    chat_json = _FakeChatJSON({
        "predicted_reason": "r", "emotional_state": "anxious", "key_objections": [],
    })
    EmailPromptBuilder().build("Report.", _make_cart(), chat_json=chat_json)

    assert len(chat_json.calls) == 1
    messages = chat_json.calls[0]
    assert isinstance(messages, list) and messages
    assert all("role" in m and "content" in m for m in messages)


def test_emotional_state_out_of_vocab_coerced_to_indecisive():
    """An emotional_state outside the 6-value vocabulary is coerced to the
    neutral 'indecisive' default rather than drifting past the contract."""
    chat_json = _FakeChatJSON({
        "predicted_reason": "Some reason",
        "emotional_state": "overwhelmed",   # not in EMOTIONAL_STATES
        "key_objections": [],
    })
    insight = EmailPromptBuilder().build("Report.", _make_cart(), chat_json=chat_json)

    assert insight.emotional_state == "indecisive"


def test_emotional_state_known_value_is_normalized_and_kept():
    """A known emotional_state is kept (and case-normalized to the canonical
    lower-case form)."""
    chat_json = _FakeChatJSON({
        "predicted_reason": "Some reason",
        "emotional_state": "Price-Sensitive",
        "key_objections": [],
    })
    insight = EmailPromptBuilder().build("Report.", _make_cart(), chat_json=chat_json)

    assert insight.emotional_state == "price-sensitive"


def test_extraction_messages_exclude_direct_cart_pii():
    """The extraction prompt must carry only non-PII signal — customer_name,
    email, and location are deliberately omitted (#7)."""
    chat_json = _FakeChatJSON({
        "predicted_reason": "r", "emotional_state": "anxious", "key_objections": [],
    })
    cart = _make_cart(
        customer_name="Alice Smith",
        email="alice@pii.example.com",
        location="London, UK",
    )
    EmailPromptBuilder().build("Report.", cart, chat_json=chat_json)

    content = " ".join(m["content"] for m in chat_json.calls[0])
    assert "Alice Smith" not in content
    assert "alice@pii.example.com" not in content
    assert "London, UK" not in content
