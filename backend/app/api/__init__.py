"""API blueprints.

The only live HTTP surface is the cart-recovery blueprint (``cart_recovery.py``),
imported directly by the app factory. The OASIS ``/graph``, ``/simulation``, and
``/report`` blueprints were removed (#24 prune): they had no live caller after the
frontend was deleted (#56) — the engine only calls ``/api/cart-recovery/*`` — while
the in-process pipeline still uses the underlying services directly.
"""
