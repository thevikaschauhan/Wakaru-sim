"""Versioned evidence projection. No network, generated memory or inferred motives."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .email_prompt_builder import AbandonmentInsight
from .shopify_formatter import ShopifyCartData

VERSION = "buyer_state_v1"
INFERENCE_VERSION = "observed_v1"
ALERT_TYPES = frozenset({"CHECKOUT_ERROR", "CONTACT_ERROR", "DELIVERY_ERROR",
    "DISCOUNT_ERROR", "INPUT_INVALID", "INPUT_REQUIRED", "INVENTORY_ERROR",
    "MERCHANDISE_ERROR", "PAYMENT_ERROR"})
EVENT_TYPES = frozenset({"page_viewed", "product_viewed", "product_added_to_cart",
    "product_removed_from_cart", "cart_viewed", "checkout_started",
    "checkout_contact_info_submitted", "checkout_address_info_submitted",
    "checkout_shipping_info_submitted", "payment_info_submitted", "checkout_completed",
    "alert_displayed", "input_changed", "input_focused", "input_blurred",
    "search_submitted", "element_hovered", "page_exit", "user_exit"})


def validate_evidence(value: object) -> list[dict]:
    """Reject malformed references rather than manufacturing event identities."""
    if not isinstance(value, list) or len(value) > 500:
        raise ValueError("evidence_events must be an array of at most 500 events")
    seen = set()
    result = []
    for event in value:
        if not isinstance(event, dict) or set(event) - {"event_id", "event_name", "occurred_at", "alert_type"}:
            raise ValueError("invalid evidence event fields")
        event_id = event.get("event_id")
        name = event.get("event_name")
        when = event.get("occurred_at")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 128 or event_id in seen:
            raise ValueError("invalid or duplicate evidence event id")
        if name not in EVENT_TYPES or not isinstance(when, str):
            raise ValueError("invalid evidence event type or timestamp")
        parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("evidence timestamps require a timezone")
        alert = event.get("alert_type")
        if alert is not None and (name != "alert_displayed" or alert not in ALERT_TYPES):
            raise ValueError("invalid diagnostic alert type")
        normalized = {"event_id": event_id, "event_name": name,
                      "occurred_at": parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
        if alert is not None:
            normalized["alert_type"] = alert
        result.append(normalized)
        seen.add(event_id)
    return sorted(result, key=lambda e: (datetime.fromisoformat(e["occurred_at"].replace("Z", "+00:00")), e["event_id"]))


def direct_insight(cart: ShopifyCartData) -> AbandonmentInsight:
    events = validate_evidence(cart.evidence_events)
    signals = sorted({e["event_name"] for e in events})
    errors = [{"code": e["alert_type"], "evidence_ids": [e["event_id"]]}
              for e in events if e.get("alert_type")]
    stages = {"checkout_started": "checkout", "checkout_contact_info_submitted": "checkout",
              "checkout_address_info_submitted": "shipping", "checkout_shipping_info_submitted": "shipping",
              "payment_info_submitted": "payment", "checkout_completed": "purchased"}
    stage = "unknown"
    for event in events:
        # An observed completed checkout cannot be undone by a late UI event.
        if stage == "purchased":
            continue
        if event["event_name"] in stages:
            stage = stages[event["event_name"]]
        elif stage == "unknown" and event["event_name"] in {"cart_viewed", "product_added_to_cart"}:
            stage = "cart"
    # This digest identifies the actual projection inputs, including the frozen cart.
    # It is a replay identity, not a signature or proof of upstream authenticity.
    canonical = json.dumps({"events": events, "cart_items": cart.cart_items,
        "cart_total": cart.cart_total, "currency": cart.currency,
        "episode_id": cart.episode_id}, sort_keys=True, separators=(",", ":"), allow_nan=False)
    state = {
        "version": VERSION, "inference_version": INFERENCE_VERSION,
        "episode_id": cart.episode_id,
        "as_of": events[-1]["occurred_at"] if events else None,
        "snapshot_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "observations": events, "journey_stage": stage, "observed_friction": errors,
        "motivation_status": "unknown",
        "purchase_readiness": {"status": "unestimated", "probability": None,
            "target": "paid_order_within_7d_without_vakaru", "model_version": None},
        "coverage": {"event_count": len(events), "captured_signals": signals,
            "missing_signals": ["cross_browser_completeness", "verified_motivation", "calibrated_readiness"]
                + ([] if events else ["event_provenance"])},
    }
    return AbandonmentInsight(
        predicted_reason="Abandonment reason unknown",
        emotional_state="unknown", recommended_angle="none", reason_category="unknown",
        key_objections=[], confidence=0.0,
        confidence_reasoning="Uncalibrated motive confidence is not used; readiness is unestimated.",
        email_prompt_context="Use the frozen cart and approved brand facts. Do not infer motives, emotions, purchases or entitlements.",
        buyer_state=state,
    )
