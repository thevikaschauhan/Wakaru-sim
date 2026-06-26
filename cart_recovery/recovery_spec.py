"""
Pure, transport-agnostic cart-recovery specification.

Holds the pieces of the cart-recovery analysis that carry no transport/HTTP
dependency — the requirement string + the confidence heuristic — used by the
in-process backend orchestrator (backend/app/services/cart_recovery_workflow.py,
issue #19). The SDK-based external path (cart_recovery/engine.py -> mirofish) that
once also shared these was removed in the #24 prune.
"""
from __future__ import annotations

from .shopify_formatter import ShopifyCartData

# The simulation requirement sent to MiroFish for every cart recovery analysis.
RECOVERY_REQUIREMENT = (
    "Simulate the psychology of a customer who abandoned their shopping cart. "
    "Analyse the seed document to understand who this customer is, what they left behind, "
    "and what likely caused them to leave. Predict the emotional and rational reasons for "
    "abandonment, identify the key objections or barriers, and determine the most effective "
    "messaging angle to bring them back to complete the purchase. "
    "Focus on human behavioural dynamics: price sensitivity, trust, urgency, social proof, "
    "and loyalty. Output actionable insights for a personalized recovery email."
)


def assess_confidence_heuristic(cart_data: ShopifyCartData) -> tuple[float, str]:
    """Deterministic fallback when no LLM client is available."""
    score = 0.3  # baseline for having cart + exit data
    reasons = []

    if getattr(cart_data, "shopper_profile", None):
        score += 0.15
        reasons.append("profile")
    if getattr(cart_data, "behavioral_memory", ""):
        score += 0.1
        reasons.append("memory")
    if len(getattr(cart_data, "form_interactions", [])) > 0:
        score += 0.1
        reasons.append("forms")
    if len(getattr(cart_data, "hover_signals", [])) > 0:
        score += 0.1
        reasons.append("hovers")
    if getattr(cart_data, "ontology_hint", None):
        score += 0.15
        reasons.append("hint")
    if len(getattr(cart_data, "recovery_history", [])) > 0:
        score += 0.1
        reasons.append("history")
    if len(getattr(cart_data, "alert_messages_shown", [])) > 0:
        score += 0.05
        reasons.append("alerts")
    if len(getattr(cart_data, "searches_submitted", [])) > 0:
        score += 0.05
        reasons.append("searches")

    score = min(score, 1.0)
    reasoning = (
        f"Heuristic score based on {', '.join(reasons) or 'minimal'} signals"
    )
    return round(score, 2), reasoning
