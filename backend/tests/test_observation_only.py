"""An UNKNOWN captured checkout cannot acquire a reason through simulation."""
from unittest.mock import Mock

from app.services import cart_recovery_workflow as wf
from cart_recovery.shopify_formatter import ShopifyCartData


def test_unknown_checkout_never_runs_simulation(monkeypatch):
    monkeypatch.setattr(wf.Config, "ZEP_API_KEY", "test")
    monkeypatch.setattr(wf.Config, "LLM_API_KEY", "test")
    cart = ShopifyCartData(
        customer_id="captured-test", customer_name="Email-derived-name", email="test@example.com",
        cart_items=[{"product": "The Collection Snowboard: Liquid", "price": 749.95, "quantity": 1}],
        cart_total=749.95, ontology_hint={"code": "UNKNOWN_ABANDONMENT", "confidence": 0.45},
        past_orders=0, is_first_order=False,
    )
    create = Mock(side_effect=AssertionError("unknown intent must not create a simulation"))
    monkeypatch.setattr(wf.ProjectManager, "create_project", create)
    progress = []
    result = wf.run_cart_recovery(cart, on_progress=lambda stage, state: progress.append((stage, state)))
    assert result.reason_category == "unknown"
    assert result.emotional_state == "unknown"
    assert result.confidence == 0
    assert result.key_objections == []
    assert "749.95" in result.email_prompt_context
    assert "Email-derived-name" not in result.email_prompt_context
    assert "test@example.com" not in result.email_prompt_context
    assert "order" not in result.email_prompt_context.lower()
    assert progress == [("observation_only_completed", {"analysis_mode": "observation_only", "reason_observed": False})]
    create.assert_not_called()
