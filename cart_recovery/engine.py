from __future__ import annotations

import os
import tempfile
from typing import Callable

from mirofish import MiroFishClient

from .email_prompt_builder import AbandonmentInsight, EmailPromptBuilder
from .shopify_formatter import ShopifyCartData, ShopifyFormatter

# The simulation requirement sent to MiroFish for every cart recovery analysis.
_RECOVERY_REQUIREMENT = (
    "Simulate the psychology of a customer who abandoned their shopping cart. "
    "Analyse the seed document to understand who this customer is, what they left behind, "
    "and what likely caused them to leave. Predict the emotional and rational reasons for "
    "abandonment, identify the key objections or barriers, and determine the most effective "
    "messaging angle to bring them back to complete the purchase. "
    "Focus on human behavioural dynamics: price sensitivity, trust, urgency, social proof, "
    "and loyalty. Output actionable insights for a personalized recovery email."
)


class CartRecoveryEngine:
    """
    High-level engine that orchestrates MiroFish to analyse a Shopify cart
    abandonment and produce structured insights for email personalisation.

    Usage:
        engine = CartRecoveryEngine(mirofish_url="http://localhost:5001")
        cart = ShopifyCartData(
            customer_id="cust_123",
            customer_name="Sarah",
            email="sarah@example.com",
            cart_items=[{"product": "Wireless Headphones", "price": 89.99, "quantity": 1}],
            cart_total=89.99,
            past_orders=0,
            exit_page="checkout/payment",
            abandoned_at_step="payment",
            device="mobile",
            location="London, UK",
        )
        insight = engine.analyze_abandonment(cart)
        print(insight.email_prompt_context)  # paste into your LLM
    """

    def __init__(
        self,
        mirofish_url: str = "http://localhost:5001",
        enable_reddit: bool = False,
        simulation_hours: int = 24,
    ):
        self._client = MiroFishClient(mirofish_url)
        self._formatter = ShopifyFormatter()
        self._prompt_builder = EmailPromptBuilder()
        self._enable_reddit = enable_reddit
        self._simulation_hours = simulation_hours

    def analyze_abandonment(
        self,
        cart: ShopifyCartData,
        on_progress: Callable[[str, object], None] | None = None,
    ) -> AbandonmentInsight:
        """
        Run the full MiroFish pipeline for a single abandoned cart event.

        Args:
            cart: Shopify cart + customer data.
            on_progress: Optional callback(stage: str, state) for progress updates.
                Stages: ontology_generated, graph_completed, simulation_ready,
                        simulation_completed, generating_report.

        Returns:
            AbandonmentInsight with predicted reason, emotional state,
            recommended email angle, key objections, and a ready-to-use LLM prompt.
        """
        seed_doc = self._formatter.format_as_seed_doc(cart)

        # Write seed doc to a temp file so MiroFishClient can upload it
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            prefix=f"cart_{cart.customer_id}_",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(seed_doc)
            tmp_path = tmp.name

        try:
            report = self._client.run_full_pipeline(
                files=[tmp_path],
                requirement=_RECOVERY_REQUIREMENT,
                enable_twitter=True,
                enable_reddit=self._enable_reddit,
                simulation_hours=self._simulation_hours,
                on_progress=on_progress,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        return self._prompt_builder.build(report.content, cart)

    def interview(self, simulation_id: str, question: str) -> str:
        """
        Ask the simulation's ReportAgent a follow-up question after analysis.
        Useful for V2 autonomous agent flows.
        """
        return self._client.interview_agent(simulation_id, question)
