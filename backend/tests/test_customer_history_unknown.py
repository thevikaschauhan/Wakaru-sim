from cart_recovery.shopify_formatter import ShopifyCartData, ShopifyFormatter
from cart_recovery.email_prompt_builder import EmailPromptBuilder


def test_missing_customer_history_remains_unknown():
    cart = ShopifyCartData(customer_id="c", customer_name="", email="a@example.com", cart_items=[], cart_total=0)
    assert cart.past_orders is None
    assert cart.is_first_order is None
    formatter = ShopifyFormatter()
    profile = formatter._customer_profile(cart)
    history = formatter._purchase_history(cart)
    assert "unknown" in history.lower()
    assert "new customer" not in profile
    assert "returning customer" not in profile
    assert "trust" not in history
    assert EmailPromptBuilder._base_angle_from_report("neutral", cart) == "gentle-reminder"


def test_zero_orders_does_not_prove_first_visit_or_trust():
    cart = ShopifyCartData(customer_id="c", customer_name="", email="a@example.com", cart_items=[], cart_total=0, past_orders=0)
    history = ShopifyFormatter()._purchase_history(cart)
    assert "0" in history
    assert "first visit" not in history
    assert "trust" not in history
