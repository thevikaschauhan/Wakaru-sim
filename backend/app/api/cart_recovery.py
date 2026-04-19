"""
Cart Recovery API Blueprint — Vakaru Integration Point
POST /api/cart-recovery/analyze
"""

import sys
import os
import logging
import traceback

from flask import Blueprint, request, jsonify
import sentry_sdk

# Add MiroFish-main root to sys.path so cart_recovery module is importable
_mirofish_root = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
if _mirofish_root not in sys.path:
    sys.path.insert(0, _mirofish_root)

from cart_recovery.engine import CartRecoveryEngine, ShopifyCartData  # noqa: E402

logger = logging.getLogger("mirofish.cart_recovery")

cart_recovery_bp = Blueprint("cart_recovery", __name__)

# Single engine instance — reused across requests
_engine: CartRecoveryEngine | None = None


def _get_engine() -> CartRecoveryEngine:
    global _engine
    if _engine is None:
        # When running standalone, MiroFish calls itself on localhost:5001
        mirofish_url = os.environ.get("MIROFISH_SELF_URL", "http://localhost:5001")
        _engine = CartRecoveryEngine(mirofish_url=mirofish_url)
    return _engine


@cart_recovery_bp.route("/analyze", methods=["POST"])
def analyze():
    """
    Analyze a cart abandonment event using MiroFish psychology simulation.

    Input JSON: ShopifyCartData fields
    Output JSON: { "success": true, "data": AbandonmentInsight }
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"success": False, "error": "Request body must be valid JSON"}), 400

    # Validate required fields
    required = ["customer_id", "email"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return jsonify({
            "success": False,
            "error": f"Missing required fields: {', '.join(missing)}"
        }), 400

    # Build ShopifyCartData from request body
    try:
        cart = ShopifyCartData(
            customer_id=body.get("customer_id", "unknown"),
            customer_name=body.get("customer_name", "Shopper"),
            email=body.get("email", ""),
            checkout_token=body.get("checkout_token"),
            cart_items=body.get("cart_items", []),
            cart_total=float(body.get("cart_total", 0.0)),
            cart_subtotal=float(body.get("cart_subtotal")) if body.get("cart_subtotal") is not None else None,
            cart_tax=float(body.get("cart_tax")) if body.get("cart_tax") is not None else None,
            currency=body.get("currency", "USD"),
            discount_codes=body.get("discount_codes", []),
            discount_amount=float(body.get("discount_amount")) if body.get("discount_amount") is not None else None,
            discount_type=body.get("discount_type"),
            shipping_cost=float(body.get("shipping_cost")) if body.get("shipping_cost") is not None else None,
            shipping_method=body.get("shipping_method"),
            shipping_country=body.get("shipping_country"),
            payment_gateway_attempted=body.get("payment_gateway_attempted"),
            payment_method_type=body.get("payment_method_type"),
            browsing_history=body.get("browsing_history", []),
            collections_viewed=body.get("collections_viewed", []),
            products_viewed=body.get("products_viewed", []),
            products_removed=body.get("products_removed", []),
            searches_submitted=body.get("searches_submitted", []),
            alert_messages_shown=body.get("alert_messages_shown", []),
            time_on_site_minutes=float(body.get("time_on_site_minutes", 0.0)),
            exit_page=body.get("exit_page", ""),
            abandoned_at_step=body.get("abandoned_at_step", ""),
            device_type=body.get("device", body.get("device_type", "unknown")),
            viewport_width=int(body.get("viewport_width")) if body.get("viewport_width") is not None else None,
            language=body.get("language"),
            market=body.get("market"),
            referral_source=body.get("referral_source", ""),
            utm_campaign=body.get("utm_campaign"),
            utm_source=body.get("utm_source"),
            past_orders=int(body.get("past_orders", 0)),
            total_spend_lifetime=float(body.get("total_spend_lifetime")) if body.get("total_spend_lifetime") is not None else None,
            is_first_order=bool(body.get("is_first_order", False)),
            customer_tags=body.get("customer_tags", []),
            email_marketing_consent=body.get("email_marketing_consent"),
            location=body.get("location", ""),
            hours_since_last_abandonment=float(body.get("hours_since_last_abandonment")) if body.get("hours_since_last_abandonment") is not None else None,
            # PIE-V2 enrichment fields
            shopper_profile=body.get("shopper_profile"),
            behavioral_memory=body.get("behavioral_memory", ""),
            form_interactions=body.get("form_interactions", []),
            hover_signals=body.get("hover_signals", []),
            ontology_hint=body.get("ontology_hint"),
            recovery_history=body.get("recovery_history", []),
            merchant_effectiveness=body.get("merchant_effectiveness"),
        )
    except (TypeError, ValueError) as e:
        logger.warning(f"Invalid cart data: {e}")
        return jsonify({"success": False, "error": f"Invalid cart data: {str(e)}"}), 400

    # Progress logging callback
    def on_progress(stage: str, message: str):
        logger.info(f"[{body.get('customer_id')}] {stage}: {message}")

    # Run analysis
    try:
        engine = _get_engine()
        insight = engine.analyze_abandonment(cart, on_progress=on_progress)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logger.error(f"Cart recovery analysis failed for {body.get('customer_id')}: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": f"Analysis failed: {str(e)}"
        }), 500

    return jsonify({
        "success": True,
        "data": {
            "predicted_reason": insight.predicted_reason,
            "emotional_state": insight.emotional_state,
            "recommended_angle": insight.recommended_angle,
            "key_objections": insight.key_objections,
            "email_prompt_context": insight.email_prompt_context,
            "confidence": insight.confidence,
            "confidence_reasoning": insight.confidence_reasoning,
        }
    }), 200
