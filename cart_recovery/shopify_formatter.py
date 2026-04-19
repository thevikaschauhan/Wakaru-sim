from __future__ import annotations

import textwrap
from dataclasses import dataclass, field


@dataclass
class ShopifyCartData:
    """Structured representation of a Shopify abandoned cart event."""

    # --- Required fields ---
    customer_id: str
    customer_name: str
    email: str
    cart_items: list[dict]          # [{product, variant, price, quantity, category}]
    cart_total: float

    # --- Cart / checkout details ---
    checkout_token: str | None = None
    currency: str = "USD"
    cart_subtotal: float | None = None
    cart_tax: float | None = None
    discount_codes: list[str] = field(default_factory=list)
    discount_amount: float | None = None
    discount_type: str | None = None
    discount_used: bool = False
    shipping_cost: float | None = None
    shipping_method: str | None = None
    shipping_country: str | None = None
    payment_gateway_attempted: str | None = None
    payment_method_type: str | None = None

    # --- Browsing / behavioral ---
    browsing_history: list[str] = field(default_factory=list)   # page titles/URLs visited
    collections_viewed: list[str] = field(default_factory=list)
    products_viewed: list[str] = field(default_factory=list)
    products_removed: list[str] = field(default_factory=list)
    searches_submitted: list[str] = field(default_factory=list)
    alert_messages_shown: list[str] = field(default_factory=list)
    time_on_site_minutes: float = 0.0
    exit_page: str = ""             # where they dropped off (e.g. "checkout/payment")
    abandoned_at_step: str = ""     # cart | information | shipping | payment

    # --- Device / traffic ---
    device_type: str = ""           # mobile | desktop | tablet
    device: str = ""                # legacy alias for device_type
    viewport_width: int | None = None
    language: str | None = None
    market: str | None = None
    referral_source: str = ""       # google | instagram | email | direct | etc.
    utm_source: str | None = None
    utm_campaign: str | None = None

    # --- Customer history ---
    past_orders: int = 0
    total_spend_lifetime: float | None = None
    is_first_order: bool = False
    customer_tags: list[str] = field(default_factory=list)
    email_marketing_consent: bool | None = None
    location: str = ""              # city, country
    hours_since_last_abandonment: float | None = None

    # --- PIE-V2 enrichment fields (all optional, backwards compatible) ---
    shopper_profile: dict | None = None         # ShopperProfileContext from engine
    behavioral_memory: str = ""
    form_interactions: list[dict] = field(default_factory=list)  # [{field, action}]
    hover_signals: list[dict] = field(default_factory=list)      # [{element, duration_ms}]
    ontology_hint: dict | None = None           # {code, confidence, reasoning}
    recovery_history: list[dict] = field(default_factory=list)   # [{angle, ontology_code, outcome, ...}]
    merchant_effectiveness: dict | None = None  # {top_angle_for_ontology, conversion_rate}

    def __post_init__(self):
        if not self.device and self.device_type:
            self.device = self.device_type


class ShopifyFormatter:
    """Converts ShopifyCartData into a rich text seed document for MiroFish."""

    def format_as_seed_doc(self, cart: ShopifyCartData) -> str:
        """
        Returns a multi-section plain-text document describing the customer and
        their abandonment context. MiroFish will extract entities and build a
        simulation around this text.
        """
        sections = [
            self._customer_profile(cart),
            self._cart_contents(cart),
            self._behavioral_signals(cart),
            self._purchase_history(cart),
            self._abandonment_context(cart),
            self._simulated_peers(cart),
        ]
        doc = "\n\n".join(s for s in sections if s.strip())

        # --- PIE-V2 enrichment sections (gated on data presence) ---

        # Returning Customer Intelligence (from shopper_profile)
        if cart.shopper_profile:
            sp = cart.shopper_profile
            doc += "\n\n## RETURNING CUSTOMER INTELLIGENCE\n"
            doc += f"Visit frequency: {sp.get('visit_count', 1)} visits"
            if sp.get('avg_days_between_visits'):
                doc += f" (avg {sp['avg_days_between_visits']:.0f} days between visits)"
            doc += "\n"
            doc += f"Journey state: {sp.get('journey_state', 'AWARENESS')}\n"
            if sp.get('checkout_high_water'):
                doc += f"Checkout high-water: reached {sp['checkout_high_water']} step\n"
            doc += f"Lifetime value: ${sp.get('lifetime_value', 0):.2f} across {sp.get('visit_count', 0)} orders\n"
            emails_sent = sp.get('emails_sent', 0)
            if emails_sent > 0:
                doc += f"Recovery emails: {emails_sent} sent, {sp.get('emails_opened', 0)} opened, {sp.get('emails_clicked', 0)} clicked\n"
            if sp.get('last_recovery_angle'):
                doc += f"Last recovery: {sp['last_recovery_angle']} angle → {sp.get('last_recovery_outcome', 'unknown')}\n"

        # Recovery History
        if cart.recovery_history:
            doc += "\n\n## RECOVERY HISTORY\n"
            for i, rh in enumerate(cart.recovery_history, 1):
                entry = f"Attempt {i} ({rh.get('days_ago', '?')} days ago): "
                entry += f"{rh.get('ontology_code', '?')} → {rh.get('angle', '?')} angle → {rh.get('outcome', '?')}"
                if rh.get('outcome') == 'converted' and rh.get('hours_to_convert'):
                    entry += f" in {rh['hours_to_convert']:.1f}h"
                doc += entry + "\n"

        # Behavioral Memory (from Zep)
        if cart.behavioral_memory and cart.behavioral_memory not in ("(Zep not configured)", "(No prior context)"):
            doc += "\n\n## BEHAVIORAL MEMORY (cross-session patterns)\n"
            doc += cart.behavioral_memory + "\n"

        # Micro-Interaction Signals
        has_micro = (cart.form_interactions or cart.hover_signals or
                     cart.alert_messages_shown or cart.searches_submitted)
        if has_micro:
            doc += "\n\n## MICRO-INTERACTION SIGNALS\n"

            if cart.form_interactions:
                doc += "Form interactions:\n"
                for fi in cart.form_interactions:
                    doc += f"  - {fi.get('field', '?')}: {fi.get('action', '?')}\n"

            if cart.hover_signals:
                doc += "Hover concerns:\n"
                for hs in cart.hover_signals:
                    dur_s = hs.get('duration_ms', 0) / 1000.0
                    doc += f"  - {hs.get('element', '?')} ({dur_s:.1f}s)\n"

            if cart.alert_messages_shown:
                doc += "Alerts/validation errors:\n"
                for msg in cart.alert_messages_shown:
                    doc += f'  - "{msg}"\n'

            if cart.searches_submitted:
                doc += "Search queries:\n"
                for q in cart.searches_submitted:
                    doc += f'  - "{q}"\n'

        # Pre-Classification Hint (Stage 1 ontology result)
        if cart.ontology_hint:
            doc += "\n\n## PRE-CLASSIFICATION HINT (Engine Stage 1)\n"
            doc += f"Classification: {cart.ontology_hint.get('code', '?')}"
            conf = cart.ontology_hint.get('confidence', 0)
            if conf > 0:
                doc += f" (confidence: {conf:.2f})"
            doc += "\n"
            if cart.ontology_hint.get('reasoning'):
                doc += f"Reasoning: {cart.ontology_hint['reasoning']}\n"
            doc += "Note: Validate or challenge this classification based on deeper simulation analysis.\n"

        # Merchant Pattern Intelligence
        if cart.merchant_effectiveness:
            doc += "\n\n## MERCHANT PATTERN INTELLIGENCE\n"
            me = cart.merchant_effectiveness
            if me.get('top_angle_for_ontology'):
                doc += f"Top converting angle for this abandonment type: {me['top_angle_for_ontology']}"
                if me.get('conversion_rate'):
                    doc += f" ({me['conversion_rate']*100:.1f}% conversion rate)"
                doc += "\n"

        return doc

    # ------------------------------------------------------------------

    def _customer_profile(self, cart: ShopifyCartData) -> str:
        loyalty = "new customer" if cart.past_orders == 0 else f"returning customer ({cart.past_orders} previous orders)"
        lines = [
            f"Customer Profile: {cart.customer_name}",
            f"",
            f"{cart.customer_name} is a {loyalty} from {cart.location or 'unknown location'}.",
            f"They accessed the store via {cart.device or 'unknown device'}{' from ' + cart.referral_source if cart.referral_source else ''}.",
        ]
        if (cart.total_spend_lifetime or 0) > 0:
            lines.append(
                f"Lifetime spend with this brand: {cart.currency} {cart.total_spend_lifetime:,.2f}."
            )
        return "\n".join(lines)

    def _cart_contents(self, cart: ShopifyCartData) -> str:
        if not cart.cart_items:
            return ""
        lines = [
            "Abandoned Cart Contents:",
            "",
        ]
        for item in cart.cart_items:
            name = item.get("product", item.get("name", "Unknown product"))
            qty = item.get("quantity", 1)
            price = item.get("price", 0)
            variant = item.get("variant", "")
            category = item.get("category", "")
            desc = f"  - {name}"
            if variant:
                desc += f" ({variant})"
            desc += f" × {qty} @ {cart.currency} {price:.2f}"
            if category:
                desc += f" [{category}]"
            lines.append(desc)

        lines.append("")
        lines.append(f"Cart total: {cart.currency} {cart.cart_total:,.2f}")
        if cart.discount_used:
            lines.append("Note: A discount code was applied during the session.")
        return "\n".join(lines)

    def _behavioral_signals(self, cart: ShopifyCartData) -> str:
        lines = [
            "Browsing Behavior:",
            "",
            f"Total time on site: {cart.time_on_site_minutes:.0f} minutes.",
        ]
        if cart.browsing_history:
            lines.append(f"Pages visited: {', '.join(cart.browsing_history[:10])}.")
        if cart.exit_page:
            lines.append(f"Last page before leaving: {cart.exit_page}.")
        if cart.abandoned_at_step:
            lines.append(
                f"Checkout step reached before abandonment: {cart.abandoned_at_step}."
            )
        return "\n".join(lines)

    def _purchase_history(self, cart: ShopifyCartData) -> str:
        if cart.past_orders == 0:
            return textwrap.dedent("""\
                Purchase History:

                This is the customer's first visit and they have never purchased from this brand before.
                They have no prior relationship or trust established with the store.""")

        return textwrap.dedent(f"""\
            Purchase History:

            {cart.customer_name} has placed {cart.past_orders} order(s) with this brand before,
            with a total lifetime spend of {cart.currency} {(cart.total_spend_lifetime or 0):,.2f}.
            They are a familiar customer who has shown willingness to buy from this brand previously.""")

    def _abandonment_context(self, cart: ShopifyCartData) -> str:
        step_descriptions = {
            "cart": "They left from the cart page, suggesting they may not have been ready to commit to the purchase.",
            "information": "They abandoned at the personal information step, which may indicate privacy concerns or friction.",
            "shipping": "They abandoned at the shipping page, suggesting shipping cost or delivery time may have been a blocker.",
            "payment": "They abandoned at the payment page — this often signals price sensitivity, payment method availability, or final hesitation.",
        }
        step_note = step_descriptions.get(
            cart.abandoned_at_step.lower(),
            "They left mid-checkout without completing their purchase."
        )
        return textwrap.dedent(f"""\
            Abandonment Context:

            {step_note}
            The customer spent {cart.time_on_site_minutes:.0f} minutes on the site before leaving,
            indicating {'significant' if cart.time_on_site_minutes > 5 else 'brief'} engagement.""")

    def _simulated_peers(self, cart: ShopifyCartData) -> str:
        """
        Adds synthetic peer entities so MiroFish creates social context agents
        that can exert psychological pressure or validation on the customer persona.
        """
        items_summary = ", ".join(
            item.get("product", "item") for item in cart.cart_items[:3]
        )
        return textwrap.dedent(f"""\
            Social Context:

            Similar Shopper A is a budget-conscious consumer who frequently compares prices across
            multiple websites before purchasing. They often abandon carts when they find better deals elsewhere.

            Similar Shopper B is an impulse buyer who responds strongly to urgency signals like
            "Only 2 left in stock" or "Sale ends tonight". They have high brand loyalty once they make
            their first purchase.

            Influencer Reviewer is a lifestyle content creator who has posted positive reviews about
            products similar to {items_summary}. Their followers trust their recommendations.

            Brand Advocate is a loyal repeat customer of this store who frequently recommends it
            to friends and family. They have made 10+ purchases and consider this brand reliable.""")
