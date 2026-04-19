from __future__ import annotations

import re
from dataclasses import dataclass, field

from .shopify_formatter import ShopifyCartData


@dataclass
class AbandonmentInsight:
    """
    Structured output from the MiroFish cart recovery analysis.
    Feed `email_prompt_context` directly into your LLM to generate the recovery email.
    """
    predicted_reason: str           # primary abandonment reason identified
    emotional_state: str            # e.g. "anxious", "indecisive", "price-sensitive"
    recommended_angle: str          # e.g. "urgency", "trust", "discount", "social-proof"
    key_objections: list[str] = field(default_factory=list)
    email_prompt_context: str = ""  # ready-to-use context block for your LLM
    confidence: float = 0.5         # 0.0-1.0 confidence in the analysis
    confidence_reasoning: str = ""  # one-sentence explanation of the score


class EmailPromptBuilder:
    """
    Converts a MiroFish simulation report + cart data into:
    1. Structured AbandonmentInsight (reason, emotion, angle, objections)
    2. A ready-to-use LLM prompt context for generating the recovery email
    """

    RECOVERY_SYSTEM_PROMPT = """\
You are an expert e-commerce email copywriter specialising in cart recovery.
Your goal is to write a short, warm, personalised email that brings the customer
back to complete their purchase. Do NOT be pushy or generic.
Use the customer context below to craft a message that speaks directly to their
situation and the specific insight from the psychology simulation."""

    def build(
        self,
        report_content: str,
        cart: ShopifyCartData,
        confidence: float = 0.5,
    ) -> AbandonmentInsight:
        """
        Parse the MiroFish report and produce an AbandonmentInsight.

        Args:
            report_content: Full markdown text from MiroFish ReportAgent.
            cart: The original ShopifyCartData for personalization.
            confidence: Analysis confidence score (0.0-1.0) from the engine.

        Returns:
            AbandonmentInsight with all fields populated.
        """
        self._confidence = confidence

        reason = self._extract_reason(report_content)
        emotional_state = self._extract_emotion(report_content)
        angle = self._choose_angle(report_content, cart)
        objections = self._extract_objections(report_content)
        prompt_context = self._build_prompt_context(
            report_content, cart, reason, emotional_state, angle, objections
        )

        insight = AbandonmentInsight(
            predicted_reason=reason,
            emotional_state=emotional_state,
            recommended_angle=angle,
            key_objections=objections,
            email_prompt_context=prompt_context,
            confidence=confidence,
        )
        return insight

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_reason(self, report: str) -> str:
        patterns = [
            r"(?:primary|main|key)\s+(?:abandonment\s+)?reason[:\s]+([^\n.]+)",
            r"(?:abandoned|left|dropped off)\s+(?:because|due to|as a result of)[:\s]+([^\n.]+)",
            r"(?:price|shipping|trust|distraction|comparison)[^\n.]{0,120}",
        ]
        for pattern in patterns:
            match = re.search(pattern, report, re.IGNORECASE)
            if match:
                return match.group(1).strip() if match.lastindex else match.group(0).strip()[:120]
        # Fallback: pull first meaningful sentence
        sentences = [s.strip() for s in report.split(".") if len(s.strip()) > 30]
        return sentences[0][:200] if sentences else "Unable to determine from simulation"

    def _extract_emotion(self, report: str) -> str:
        emotion_keywords = {
            "anxious": ["anxious", "anxiety", "worried", "nervous", "uncertain"],
            "price-sensitive": ["price", "expensive", "cost", "afford", "budget", "cheap"],
            "indecisive": ["undecided", "indecisive", "hesitant", "unsure", "deliberating"],
            "distracted": ["distracted", "interrupted", "busy", "time", "forgot"],
            "comparison-shopping": ["comparing", "alternatives", "elsewhere", "other stores", "competition"],
            "trust-lacking": ["trust", "risk", "secure", "safe", "scam", "unknown brand"],
        }
        report_lower = report.lower()
        scores = {}
        for emotion, keywords in emotion_keywords.items():
            scores[emotion] = sum(report_lower.count(kw) for kw in keywords)
        top = max(scores, key=scores.get)
        return top if scores[top] > 0 else "indecisive"

    def _choose_angle(self, report: str, cart: ShopifyCartData) -> str:
        """Pick the email persuasion angle, rotating away from failed prior attempts."""

        # Collect angles that were tried but did not convert
        tried_angles: set[str] = set()
        if hasattr(cart, "recovery_history") and cart.recovery_history:
            for rh in cart.recovery_history:
                if rh.get("outcome") in ("ignored", "opened"):
                    tried_angles.add(rh.get("angle", ""))

        # Check merchant-level effectiveness data
        merchant_preferred: str | None = None
        if hasattr(cart, "merchant_effectiveness") and cart.merchant_effectiveness:
            merchant_preferred = cart.merchant_effectiveness.get(
                "top_angle_for_ontology"
            )

        # Keyword-based selection from the report
        base_angle = self._base_angle_from_report(report, cart)

        # If the base angle was already tried and failed, rotate to an untried one
        if base_angle in tried_angles:
            alternatives = [
                "discount-or-value",
                "trust-and-social-proof",
                "urgency-scarcity",
                "gentle-reminder",
                "welcome-and-reassurance",
            ]
            for alt in alternatives:
                if alt not in tried_angles and alt != base_angle:
                    return alt

        # If merchant has a known effective angle, prefer it (unless already failed)
        if merchant_preferred and merchant_preferred not in tried_angles:
            return merchant_preferred

        return base_angle

    @staticmethod
    def _base_angle_from_report(report: str, cart: ShopifyCartData) -> str:
        """Keyword-based angle selection from the MiroFish report text."""
        report_lower = report.lower()

        if any(w in report_lower for w in ["price", "expensive", "cost", "discount", "afford"]):
            return "discount-or-value"
        if any(w in report_lower for w in ["trust", "safe", "secure", "risk", "unknown"]):
            return "trust-and-social-proof"
        if any(w in report_lower for w in ["stock", "limited", "sold out", "last chance", "urgent"]):
            return "urgency-scarcity"
        if cart.past_orders == 0:
            return "welcome-and-reassurance"
        if cart.past_orders >= 3:
            return "loyalty-and-reward"
        return "gentle-reminder"

    def _extract_objections(self, report: str) -> list[str]:
        objections = []
        patterns = [
            r"objection[s]?[:\-\s]+([^\n]+)",
            r"concern[s]?[:\-\s]+([^\n]+)",
            r"barrier[s]?[:\-\s]+([^\n]+)",
            r"(?:customer|shopper)\s+(?:worried|concerned)\s+about\s+([^\n.]+)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, report, re.IGNORECASE):
                text = match.group(1).strip()[:150]
                if text and text not in objections:
                    objections.append(text)
        return objections[:5]

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    def _build_prompt_context(
        self,
        report: str,
        cart: ShopifyCartData,
        reason: str,
        emotional_state: str,
        angle: str,
        objections: list[str],
    ) -> str:
        items_text = "\n".join(
            f"  - {item.get('product', 'item')} × {item.get('quantity', 1)} "
            f"({cart.currency} {item.get('price', 0):.2f})"
            for item in cart.cart_items
        )
        objections_text = (
            "\n".join(f"  - {o}" for o in objections)
            if objections
            else "  - None clearly identified"
        )

        # Trim report to avoid context overflow (keep first 1000 chars)
        report_excerpt = report[:1000].strip() + ("..." if len(report) > 1000 else "")

        angle_instructions = {
            "discount-or-value": "Offer a small discount or highlight the value/savings. Make them feel it's a smart buy.",
            "trust-and-social-proof": "Emphasise reviews, guarantees, return policy, and brand reliability.",
            "urgency-scarcity": "Mention limited stock or time-limited offer. Keep it honest and not manipulative.",
            "welcome-and-reassurance": "Welcome them warmly, address first-time buyer concerns, offer easy returns.",
            "loyalty-and-reward": "Acknowledge their loyalty, make them feel valued, perhaps offer a loyalty perk.",
            "gentle-reminder": "Friendly, no-pressure nudge. Remind them what they left behind.",
        }

        context = f"""{self.RECOVERY_SYSTEM_PROMPT}

---

CUSTOMER CONTEXT:
- Name: {cart.customer_name}
- Location: {cart.location or "Unknown"}
- Device: {cart.device or "Unknown"}
- Previous orders: {cart.past_orders}
- Referral source: {cart.referral_source or "Direct"}

ABANDONED CART:
{items_text}
Total: {cart.currency} {cart.cart_total:,.2f}
Abandoned at: {cart.abandoned_at_step or cart.exit_page or "unknown step"}

PSYCHOLOGY SIMULATION FINDINGS:
Predicted abandonment reason: {reason}
Customer emotional state: {emotional_state}
Key objections identified:
{objections_text}

Simulation report excerpt:
{report_excerpt}

EMAIL STRATEGY:
Recommended angle: {angle}
Instruction: {angle_instructions.get(angle, "Write a friendly, personalised recovery email.")}
"""

        # Recovery history context
        if hasattr(cart, "recovery_history") and cart.recovery_history:
            context += "\nPREVIOUS RECOVERY ATTEMPTS:\n"
            for rh in cart.recovery_history:
                context += (
                    f"- {rh.get('days_ago', '?')} days ago: "
                    f"{rh.get('angle', '?')} angle -> {rh.get('outcome', '?')}\n"
                )
            context += (
                "IMPORTANT: Do NOT repeat the same messaging approach "
                "as failed attempts.\n"
            )

        # Confidence-gated tone guidance
        confidence = getattr(self, "_confidence", None)
        if confidence is not None:
            if confidence >= 0.8:
                context += (
                    "\nTONE: High confidence in analysis. Be direct and "
                    "specific about the abandonment reason.\n"
                )
            elif confidence >= 0.5:
                context += (
                    "\nTONE: Moderate confidence. Address the likely reason "
                    "but keep messaging broad enough to cover alternatives.\n"
                )
            else:
                context += (
                    "\nTONE: Low confidence in specific reason. Use a gentle, "
                    "exploratory approach. Focus on value and relationship "
                    "rather than specific objection handling.\n"
                )

        # Merchant effectiveness context
        if hasattr(cart, "merchant_effectiveness") and cart.merchant_effectiveness:
            me = cart.merchant_effectiveness
            if me.get("top_angle_for_ontology") and me.get("conversion_rate"):
                context += (
                    f"\nMERCHANT DATA: {me['top_angle_for_ontology']} angle has "
                    f"{me['conversion_rate'] * 100:.0f}% conversion rate for this "
                    f"type of abandonment at this store.\n"
                )

        context += """
---

Write a recovery email (subject line + body) using this context.
Keep it under 200 words. Be personal, warm, and specific to this customer.
Do NOT use generic phrases like "You left something behind" as the opening.
"""
        return context
