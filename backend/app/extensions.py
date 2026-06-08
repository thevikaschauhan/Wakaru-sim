"""Shared Flask extension singletons (issue #12).

The Limiter is module-level so route modules (e.g. api/cart_recovery.py) can
decorate views with @limiter.limit(...) without importing the app factory.
create_app() configures storage and calls limiter.init_app(app).
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Per-IP rate limiter. Storage (Redis vs in-memory) and enablement are set on the
# app config in create_app(); no app-wide default limits — only the paid
# cart-recovery POSTs are limited, so the high-frequency GET poll and /health are
# never throttled.
limiter = Limiter(key_func=get_remote_address)
