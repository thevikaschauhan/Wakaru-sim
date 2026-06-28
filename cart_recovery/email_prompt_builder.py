from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .shopify_formatter import ShopifyCartData

logger = logging.getLogger("mirofish.email_prompt_builder")

# A chat_json-style callable — takes an OpenAI-style messages list and returns a
# parsed JSON dict (the signature of LLMClient.chat_json). Injected into build()
# so cart_recovery/ stays free of backend.app (LLMClient) imports (#47 Fork B1).
ChatJSONFn = Callable[[list[dict[str, str]]], dict[str, Any]]


# Canonical reason_category enum (Inkwell M2 contract, issue Wakaru#3). Exactly
# these 7 values — Inkwell's planner heuristic table is keyed on this set, so any
# value outside it is coerced to "unknown" on Inkwell's side regardless. The
# categorical companion to the free-form predicted_reason: predicted_reason is
# the human-readable reasoning, reason_category is what the planner pattern-matches.
REASON_CATEGORIES = (
    "shipping_cost",
    "price_sensitivity",
    "sizing_doubt",
    "payment_friction",
    "just_browsing",
    "out_of_stock_concern",
    "unknown",
)

# Canonical emotional_state vocabulary. The heuristic _extract_emotion only ever
# emits these 6 (its emotion_keywords dict keys must stay in sync); the LLM
# extraction path coerces any out-of-vocabulary value back into this set so a
# novel model output cannot drift past the AbandonmentInsight contract.
EMOTIONAL_STATES = (
    "anxious",
    "price-sensitive",
    "indecisive",
    "distracted",
    "comparison-shopping",
    "trust-lacking",
)


@dataclass
class AbandonmentInsight:
    """
    Structured output from the MiroFish cart recovery analysis.
    Feed `email_prompt_context` directly into your LLM to generate the recovery email.
    """
    predicted_reason: str           # primary abandonment reason identified
    emotional_state: str            # e.g. "anxious", "indecisive", "price-sensitive"
    recommended_angle: str          # e.g. "urgency", "trust", "discount", "social-proof"
    # Categorical form of predicted_reason, drawn from REASON_CATEGORIES (Wakaru#3).
    # Defaults to "unknown" so the field is never null/empty even on a fail-safe path.
    reason_category: str = "unknown"
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

    # Structured-insight extraction prompt (#47 Fork A1). Replaces regex prose-
    # scraping with one JSON LLM call for the fragile fields. English + Vakaru
    # cart-recovery framing; emotional_state is steered to the known vocabulary.
    INSIGHT_EXTRACTION_SYSTEM_PROMPT = """\
You extract a structured cart-abandonment summary for a Shopify cart-recovery
system. You are given the finished psychology-simulation report for one
abandoned cart plus the cart contents. Identify WHY the shopper abandoned.

Return ONLY a JSON object with exactly these keys:
- "predicted_reason": one concise sentence naming the single most likely
  abandonment reason (e.g. "Shipping cost shock at checkout").
- "emotional_state": the shopper's dominant emotional state, chosen from:
  anxious, price-sensitive, indecisive, distracted, comparison-shopping,
  trust-lacking.
- "key_objections": a JSON array of 0-5 short, clean objection phrases the
  shopper likely had (e.g. "$18.99 shipping on a $45 order"). Each entry must be
  a standalone phrase — never a report heading or a sentence fragment.

Output the JSON object only, with no commentary or markdown."""

    # Cap the report text sent to the extractor. ReportAgent markdown is a few KB;
    # 12000 chars (~3K tokens at ~4 chars/token) is a generous guard against a
    # runaway report overflowing the model context.
    _MAX_REPORT_CHARS_FOR_EXTRACTION = 12000

    def build(
        self,
        report_content: str,
        cart: ShopifyCartData,
        confidence: float = 0.5,
        *,
        chat_json: ChatJSONFn | None = None,
    ) -> AbandonmentInsight:
        """
        Parse the MiroFish report and produce an AbandonmentInsight.

        Args:
            report_content: Full markdown text from MiroFish ReportAgent.
            cart: The original ShopifyCartData for personalization.
            confidence: Analysis confidence score (0.0-1.0) from the engine.
            chat_json: Optional chat_json-style callable (#47 Fork A1/B1). When
                provided, the fragile prose fields (predicted_reason,
                emotional_state, key_objections) are extracted via one JSON LLM
                call; on ANY extraction failure build() falls back to the regex
                heuristics so the paid analysis is never lost (Fork D). When None
                (the default), the pre-#47 heuristic path is used unchanged.

        Returns:
            AbandonmentInsight with all fields populated.
        """
        self._confidence = confidence

        reason: str | None = None
        emotional_state: str | None = None
        objections: list[str] | None = None
        if chat_json is not None:
            try:
                reason, emotional_state, objections = self._extract_insight_llm(
                    report_content, cart, chat_json
                )
            except Exception as exc:  # noqa: BLE001 - degrade, never fail the paid analysis
                # Log the exception TYPE only — the message can echo raw LLM
                # output (report-derived, potentially PII), so keep it off logs.
                logger.warning(
                    "structured insight extraction failed (%s); "
                    "falling back to heuristics",
                    type(exc).__name__,
                )
        if reason is None:
            # No extractor injected, or extraction failed: heuristic fallback.
            reason = self._extract_reason(report_content)
            emotional_state = self._extract_emotion(report_content)
            objections = self._extract_objections(report_content)
        # Invariant: both branches above always set these (fail loud if not —
        # a bare assert would be stripped under python -O).
        if emotional_state is None or objections is None:
            raise RuntimeError("BUG: insight fallback left fields unset")

        # reason_category stays the deterministic classifier (#47 Fork C / #3) —
        # always one of the 7 REASON_CATEGORIES, never a raw LLM string — and the
        # angle keeps its recovery-history rotation + merchant-preference logic.
        angle = self._choose_angle(report_content, cart)
        reason_category = self._classify_reason_category(report_content, reason)
        prompt_context = self._build_prompt_context(
            report_content, cart, reason, emotional_state, angle, objections
        )

        insight = AbandonmentInsight(
            predicted_reason=reason,
            emotional_state=emotional_state,
            recommended_angle=angle,
            reason_category=reason_category,
            key_objections=objections,
            email_prompt_context=prompt_context,
            confidence=confidence,
        )
        return insight

    # ------------------------------------------------------------------
    # Structured (LLM) extraction — #47 Fork A1
    # ------------------------------------------------------------------

    def _extract_insight_llm(
        self,
        report: str,
        cart: ShopifyCartData,
        chat_json: ChatJSONFn,
    ) -> tuple[str, str, list[str]]:
        """Extract the fragile prose fields via one structured JSON LLM call.

        Returns ``(predicted_reason, emotional_state, key_objections)``. Raises
        on a missing/empty gating field (predicted_reason or emotional_state) or
        a non-dict result, so build()'s fail-safe degrades to the heuristics.
        emotional_state is coerced into EMOTIONAL_STATES.

        Direct cart PII (customer_name/email/location) is deliberately omitted —
        it does not help identify WHY the cart was abandoned, keeping this call
        off the PII surface (#7). The report excerpt is the same already-LLM-
        generated report already sent to the email-generation prompt, so it adds
        no new data exposure. Merchant-controlled text (product names, the
        report) is user-influenced; product names are whitespace-collapsed as a
        basic prompt-injection guard, while the report is trusted as
        already-generated content.
        """
        report_excerpt = report[: self._MAX_REPORT_CHARS_FOR_EXTRACTION]
        items_text = "\n".join(
            f"  - {' '.join(str(item.get('product', 'item')).split())}"
            f" x {item.get('quantity', 1)}"
            for item in cart.cart_items
        )
        # Whitespace-collapse the merchant/Shopify-controlled step field too, for
        # the same prompt-injection reason as the product names above.
        abandoned_at = " ".join(
            str(cart.abandoned_at_step or cart.exit_page or "unknown step").split()
        )
        user_message = (
            f"SIMULATION REPORT:\n{report_excerpt}\n\n"
            f"ABANDONED CART:\n{items_text}\n"
            f"Total: {cart.currency} {cart.cart_total:.2f}\n"
            f"Abandoned at: {abandoned_at}"
        )
        messages = [
            {"role": "system", "content": self.INSIGHT_EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        result = chat_json(messages)
        if not isinstance(result, dict):
            raise ValueError("extraction result is not a JSON object")

        reason = result.get("predicted_reason")
        emotional_state = result.get("emotional_state")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("extraction missing predicted_reason")
        if not isinstance(emotional_state, str) or not emotional_state.strip():
            raise ValueError("extraction missing emotional_state")

        # Coerce emotional_state into the known vocabulary (the heuristic path
        # only ever emits these 6); a novel LLM value falls back to the neutral
        # "indecisive" default rather than drifting past the contract.
        emotion = emotional_state.strip().lower()
        if emotion not in EMOTIONAL_STATES:
            emotion = "indecisive"

        # key_objections is best-effort: a malformed/absent value yields [] (the
        # clean "none identified" representation) rather than reintroducing the
        # regex fragments that #47 is about.
        objections = self._clean_objections(result.get("key_objections"))
        return reason.strip(), emotion, objections

    @staticmethod
    def _clean_objections(raw: object) -> list[str]:
        """Normalise extracted objections: keep string phrases only, trim, drop
        empties, de-duplicate (order-preserving), cap at 5 (matching the
        heuristic extractor's cap). A non-list value yields []."""
        if not isinstance(raw, list):
            return []
        cleaned: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned[:5]

    # ------------------------------------------------------------------
    # Heuristic extraction helpers (the #47 fail-safe fallback)
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
        # Keys must stay in sync with EMOTIONAL_STATES (the LLM path's vocabulary).
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

    def _classify_reason_category(self, report: str, reason: str) -> str:
        """Map the free-form predicted reason to the categorical reason_category
        enum (Inkwell M2 contract, Wakaru#3).

        Deterministic keyword classifier — no LLM call, mirroring _extract_emotion
        and _base_angle_from_report. Scores each category over the report text plus
        the already-extracted reason (so the category stays consistent with
        predicted_reason), then picks the highest-scoring category. An ambiguous or
        signal-free report falls back to "unknown" rather than guessing. A
        keyword-only classifier is the spec's accepted v1 (Inkwell's
        wakaru_reason_category_spec.md, Option 2); the returned value is always one
        of REASON_CATEGORIES, so it never needs out-of-enum coercion.

        Dict insertion order is the tie-break: more specific categories come first
        so they win when a report mixes overlapping cost/price/shipping language.
        """
        text = f"{reason}\n{report}".lower()
        category_keywords = {
            "payment_friction": [
                # Multi-word phrases only — bare "declined"/"gateway" fire on
                # non-payment prose ("declined the survey", "gateway.shopify.com").
                "payment failed", "payment declined", "card declined",
                "payment error", "checkout error", "payment gateway", "could not pay",
                "unable to pay", "transaction failed",
            ],
            "out_of_stock_concern": [
                # No bare "inventory" — it fires on "inventory management system".
                "out of stock", "out-of-stock", "sold out", "back order",
                "backorder", "low stock", "limited stock", "restock",
            ],
            "sizing_doubt": [
                # " fit" (leading space) catches the standalone word incl. punctuated
                # forms ("poor fit.") without matching outfit/benefit/fitness.
                "size", "sizing", " fit", "true to size", "too big", "too small",
                "dimension", "measurement", "wrong size",
            ],
            "shipping_cost": [
                # No "delivery time": slow delivery is a speed complaint, not a cost
                # one, and has no home in the 7-enum — let it fall to "unknown".
                "shipping cost", "shipping fee", "shipping price", "high shipping",
                "expensive shipping", "delivery cost", "shipping",
                "free shipping", "freight",
            ],
            "price_sensitivity": [
                "too expensive", "overpriced", "expensive", "afford", "budget",
                "pricey", "price", "discount", "cheaper", "sticker shock",
            ],
            "just_browsing": [
                "just browsing", "just looking", "comparing", "comparison",
                "window shopping", "researching", "exploring", "gift",
            ],
        }
        scores = {
            category: sum(text.count(kw) for kw in keywords)
            for category, keywords in category_keywords.items()
        }
        top = max(scores, key=scores.get)
        return top if scores[top] > 0 else "unknown"

    def _choose_angle(self, report: str, cart: ShopifyCartData) -> str:
        """Pick the email persuasion angle, rotating away from failed prior attempts."""

        # Collect angles that were tried but did not convert
        tried_angles: set[str] = set()
        if hasattr(cart, "recovery_history") and cart.recovery_history:
            for rh in cart.recovery_history:
                if rh.get("outcome") in ("ignored", "opened"):
                    angle = rh.get("angle")
                    if angle:
                        tried_angles.add(angle)

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
                "loyalty-and-reward",
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
        # Collapse whitespace in the merchant-controlled product name (matching
        # _extract_insight_llm) so a crafted name can't inject newlines into the
        # downstream email-generation prompt.
        items_text = "\n".join(
            f"  - {' '.join(str(item.get('product', 'item')).split())} "
            f"× {item.get('quantity', 1)} "
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

        # Whitespace-collapse the step field (matching _extract_insight_llm).
        abandoned_at = " ".join(
            str(cart.abandoned_at_step or cart.exit_page or "unknown step").split()
        )

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
Abandoned at: {abandoned_at}

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
