"""
Shopify Cart Recovery Example
------------------------------
Demonstrates using CartRecoveryEngine to analyse an abandoned cart and
produce a personalised email prompt.

Prerequisites:
    1. MiroFish backend running:  cd backend && python run.py
    2. pip install ../../client   (installs mirofish package)
    3. Set env vars: LLM_API_KEY, LLM_BASE_URL, LLM_MODEL_NAME, ZEP_API_KEY

Run:
    python cart_recovery_example.py
"""

import sys
import os

# Allow running from the examples directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from cart_recovery import CartRecoveryEngine, ShopifyCartData


def on_progress(stage: str, state) -> None:
    print(f"  [{stage}] {getattr(state, 'status', state)}")


def main():
    # -------------------------------------------------------------------
    # 1. Build a mock Shopify cart abandonment event
    # -------------------------------------------------------------------
    cart = ShopifyCartData(
        customer_id="cust_7821",
        customer_name="Sarah Mitchell",
        email="sarah.mitchell@email.com",
        cart_items=[
            {
                "product": "Wireless Noise-Cancelling Headphones",
                "variant": "Midnight Black",
                "price": 149.99,
                "quantity": 1,
                "category": "Electronics",
            },
            {
                "product": "USB-C Charging Cable (3-pack)",
                "price": 19.99,
                "quantity": 1,
                "category": "Accessories",
            },
        ],
        cart_total=169.98,
        currency="USD",
        browsing_history=[
            "Homepage",
            "Headphones category",
            "Wireless Noise-Cancelling Headphones (product page)",
            "Reviews section",
            "Cart",
            "Checkout / Shipping",
            "Checkout / Payment",
        ],
        time_on_site_minutes=14.5,
        exit_page="checkout/payment",
        abandoned_at_step="payment",
        past_orders=2,
        total_spend_lifetime=210.50,
        location="Austin, TX, USA",
        device="mobile",
        referral_source="instagram",
        discount_used=False,
    )

    # -------------------------------------------------------------------
    # 2. Run MiroFish analysis
    # -------------------------------------------------------------------
    print("\nStarting MiroFish cart psychology simulation...")
    print("This runs the full 5-step pipeline (graph → agents → sim → report).\n")

    engine = CartRecoveryEngine(
        mirofish_url="http://localhost:5001",
        enable_reddit=False,   # Twitter-only for faster V1
        simulation_hours=24,
    )

    insight = engine.analyze_abandonment(cart, on_progress=on_progress)

    # -------------------------------------------------------------------
    # 3. Display results
    # -------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("ABANDONMENT INSIGHT")
    print("=" * 60)
    print(f"Predicted reason   : {insight.predicted_reason}")
    print(f"Emotional state    : {insight.emotional_state}")
    print(f"Recommended angle  : {insight.recommended_angle}")
    print(f"Key objections     :")
    for obj in insight.key_objections:
        print(f"  - {obj}")

    print("\n" + "=" * 60)
    print("EMAIL LLM PROMPT CONTEXT (paste into GPT-4 / Claude)")
    print("=" * 60)
    print(insight.email_prompt_context)

    # -------------------------------------------------------------------
    # 4. (Optional) Generate email via OpenAI / Anthropic
    # -------------------------------------------------------------------
    # import anthropic
    # client = anthropic.Anthropic()
    # message = client.messages.create(
    #     model="claude-opus-4-6",
    #     max_tokens=512,
    #     messages=[{"role": "user", "content": insight.email_prompt_context}],
    # )
    # print("\nGenerated Recovery Email:")
    # print(message.content[0].text)


if __name__ == "__main__":
    main()
