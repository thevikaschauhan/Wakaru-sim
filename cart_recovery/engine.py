from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Callable

from mirofish import MiroFishClient

from .email_prompt_builder import AbandonmentInsight, EmailPromptBuilder
from .shopify_formatter import ShopifyCartData, ShopifyFormatter

logger = logging.getLogger("mirofish.cart_recovery")

# Prompt used to assess confidence in the cart abandonment analysis.
_CONFIDENCE_PROMPT = """\
Based on the simulation you just analyzed, assess your confidence in this cart \
abandonment analysis on a scale of 0.0 to 1.0.

SIMULATION REPORT:
{report_text}

AVAILABLE DATA SIGNALS:
- Shopper profile available: {has_profile}
- Behavioral memory available: {has_memory}
- Form interactions captured: {form_count}
- Hover signals captured: {hover_count}
- Alert messages captured: {alert_count}
- Search queries captured: {search_count}
- Pre-classification hint provided: {has_hint}
- Recovery history entries: {recovery_count}

Consider:
1. DATA COMPLETENESS: More signals = higher confidence. Only page views + exit = low confidence.
2. SIGNAL CLARITY: Did signals clearly point to one cause, or were they ambiguous?
3. SIMULATION CONVERGENCE: Did agents reach consensus on the abandonment reason?
4. HISTORICAL VALIDATION: If a pre-classification hint was provided, does the deep analysis \
confirm or contradict it? Agreement = higher confidence.

Return ONLY a JSON object:
{{"confidence": 0.XX, "reasoning": "one sentence explaining the score"}}"""

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
        llm_client=None,
    ):
        self._client = MiroFishClient(mirofish_url)
        self._formatter = ShopifyFormatter()
        self._prompt_builder = EmailPromptBuilder()
        self._enable_reddit = enable_reddit
        self._simulation_hours = simulation_hours
        self._llm_client = llm_client  # Optional; enables confidence scoring

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

        # Assess confidence in the analysis (requires an LLM client)
        confidence, confidence_reasoning = self._assess_confidence(
            report.content, cart
        )

        insight = self._prompt_builder.build(
            report.content, cart, confidence=confidence
        )
        insight.confidence_reasoning = confidence_reasoning
        return insight

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def _assess_confidence(
        self, report_text: str, cart_data: ShopifyCartData
    ) -> tuple[float, str]:
        """
        Assess confidence in the cart abandonment analysis.

        Uses an LLM call when ``self._llm_client`` is available; otherwise
        falls back to a deterministic heuristic based on signal counts.

        Returns:
            (confidence: float, reasoning: str)
        """
        if self._llm_client is not None:
            return self._assess_confidence_llm(report_text, cart_data)
        return self._assess_confidence_heuristic(cart_data)

    def _assess_confidence_llm(
        self, report_text: str, cart_data: ShopifyCartData
    ) -> tuple[float, str]:
        """LLM-based confidence assessment."""
        prompt = _CONFIDENCE_PROMPT.format(
            report_text=report_text[:2000],
            has_profile=bool(getattr(cart_data, "shopper_profile", None)),
            has_memory=bool(getattr(cart_data, "behavioral_memory", "")),
            form_count=len(getattr(cart_data, "form_interactions", [])),
            hover_count=len(getattr(cart_data, "hover_signals", [])),
            alert_count=len(getattr(cart_data, "alert_messages_shown", [])),
            search_count=len(getattr(cart_data, "searches_submitted", [])),
            has_hint=bool(getattr(cart_data, "ontology_hint", None)),
            recovery_count=len(getattr(cart_data, "recovery_history", [])),
        )
        try:
            response = self._llm_client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=200,
            )
            result = json.loads(response)
            return result.get("confidence", 0.5), result.get("reasoning", "")
        except Exception as e:
            logger.warning(f"Confidence assessment failed: {e}")
            return 0.5, "Assessment failed — defaulting to uncertain"

    @staticmethod
    def _assess_confidence_heuristic(cart_data: ShopifyCartData) -> tuple[float, str]:
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

        score = min(score, 1.0)
        reasoning = (
            f"Heuristic score based on {', '.join(reasons) or 'minimal'} signals"
        )
        return round(score, 2), reasoning

    def interview(self, simulation_id: str, question: str) -> str:
        """
        Ask the simulation's ReportAgent a follow-up question after analysis.
        Useful for V2 autonomous agent flows.
        """
        return self._client.interview_agent(simulation_id, question)
