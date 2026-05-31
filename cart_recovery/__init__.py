"""Cart-recovery package.

``CartRecoveryEngine`` is exported lazily (PEP 562 ``__getattr__``) so that
importing the PURE submodules (``shopify_formatter`` / ``email_prompt_builder``
/ ``recovery_spec``) does NOT eagerly import ``engine.py`` -> ``mirofish``.
This keeps the in-process backend orchestrator (issue #19) free of the
SDK/HTTP dependency, while ``from cart_recovery import CartRecoveryEngine``
still works unchanged for external SDK users.
"""
from .email_prompt_builder import AbandonmentInsight, EmailPromptBuilder
from .shopify_formatter import ShopifyCartData, ShopifyFormatter

__all__ = [
    "CartRecoveryEngine",
    "ShopifyCartData",
    "ShopifyFormatter",
    "AbandonmentInsight",
    "EmailPromptBuilder",
]


def __getattr__(name: str):
    # Lazy import: only pull engine.py (-> mirofish) when CartRecoveryEngine is
    # actually accessed, so pure-submodule imports stay dependency-light (#19).
    if name == "CartRecoveryEngine":
        from .engine import CartRecoveryEngine

        return CartRecoveryEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
