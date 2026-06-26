"""Shared Flask extension singletons (issue #12).

The Limiter is module-level so route modules (e.g. api/cart_recovery.py) can
decorate views with @limiter.limit(...) without importing the app factory.
create_app() configures storage and calls limiter.init_app(app).
"""
from flask import g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


def _merchant_or_ip_key():
    """Rate-limit bucket key. Per-merchant (#24) for the paid cart-recovery
    POSTs — resolve_merchant_id (app factory) binds g.merchant_id before the
    limiter check runs — so one merchant's burst cannot spend another's budget,
    which a single shared per-IP bucket could not prevent (all engine traffic
    shares one source IP). Falls back to the client IP for any request with no
    bound merchant (e.g. an unauthenticated probe)."""
    merchant_id = getattr(g, "merchant_id", None)
    if merchant_id:
        return f"merchant:{merchant_id}"
    return get_remote_address()


# Per-merchant (falling back to per-IP) rate limiter. Storage (Redis vs in-memory)
# and enablement are set on the app config in create_app(); no app-wide default
# limits — only the paid cart-recovery POSTs are limited, so the high-frequency
# GET poll and /health are never throttled.
limiter = Limiter(key_func=_merchant_or_ip_key)
