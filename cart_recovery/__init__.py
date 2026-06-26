"""Cart-recovery package.

The pure modules used by the in-process backend orchestrator (issue #19):
``shopify_formatter``, ``email_prompt_builder``, ``recovery_spec``. The
SDK-based ``CartRecoveryEngine`` (engine.py -> mirofish) was removed with the
mirofish client SDK (#24 prune) — the pre-#19 self-HTTP standalone path is gone.
"""
from .email_prompt_builder import (
    REASON_CATEGORIES,
    AbandonmentInsight,
    EmailPromptBuilder,
)
from .shopify_formatter import ShopifyCartData, ShopifyFormatter

__all__ = [
    "ShopifyCartData",
    "ShopifyFormatter",
    "AbandonmentInsight",
    "EmailPromptBuilder",
    "REASON_CATEGORIES",
]
