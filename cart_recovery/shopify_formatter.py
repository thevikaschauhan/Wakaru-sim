from __future__ import annotations

import textwrap
from dataclasses import dataclass, field


@dataclass
class ShopifyCartData:
    """Structured representation of a Shopify abandoned cart event."""

    customer_id: str
    customer_name: str
    email: str
    cart_items: list[dict]          # [{product, variant, price, quantity, category}]
    cart_total: float
    currency: str = "USD"
    browsing_history: list[str] = field(default_factory=list)   # page titles/URLs visited
    time_on_site_minutes: float = 0.0
    exit_page: str = ""             # where they dropped off (e.g. "checkout/payment")
    past_orders: int = 0
    total_spend_lifetime: float = 0.0
    location: str = ""              # city, country
    device: str = ""               # mobile | desktop | tablet
    referral_source: str = ""      # google | instagram | email | direct | etc.
    discount_used: bool = False
    abandoned_at_step: str = ""    # cart | information | shipping | payment


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
        return "\n\n".join(s for s in sections if s.strip())

    # ------------------------------------------------------------------

    def _customer_profile(self, cart: ShopifyCartData) -> str:
        loyalty = "new customer" if cart.past_orders == 0 else f"returning customer ({cart.past_orders} previous orders)"
        lines = [
            f"Customer Profile: {cart.customer_name}",
            f"",
            f"{cart.customer_name} is a {loyalty} from {cart.location or 'unknown location'}.",
            f"They accessed the store via {cart.device or 'unknown device'}{' from ' + cart.referral_source if cart.referral_source else ''}.",
        ]
        if cart.total_spend_lifetime > 0:
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
            with a total lifetime spend of {cart.currency} {cart.total_spend_lifetime:,.2f}.
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
