"""Mode-aware readiness. Never invoke paid inference from a health probe."""
import os
from pathlib import Path
from app.services.recovery_admission import AdmissionConfigurationError, approved_direct_merchants


def analysis_readiness(config):
    try:
        approved_direct_merchants()
    except AdmissionConfigurationError:
        return {'analysis': 'unavailable'}
    checks = {'analysis': 'ok'}

    intelligence = os.getenv('TYPESAFE_RECOVERY_MODE', 'off')
    if intelligence not in {'off', 'observe', 'assist', 'enforce'}:
        checks['intelligence_configuration'] = 'unavailable'
    elif intelligence != 'off' and os.getenv('TYPESAFE_RECOVERY_MERCHANTS', '').strip():
        # Local prerequisites only. Deployment policy, actual provider outcomes
        # and journal availability remain fenced by the per-request workflow.
        from cart_recovery.intelligence.typesafe import ENDPOINT
        model = os.getenv('TYPESAFE_MODEL', 'typesafe/jev-1.13')
        approved = os.getenv('TYPESAFE_APPROVED_MODELS', '').split(',')
        path = os.getenv('TYPESAFE_JOURNAL_PATH', '')
        valid = (bool(os.getenv('OPENROUTER_API_KEY', '').strip()) and model in approved
                 and bool(path) and Path(path).is_absolute()
                 and os.getenv('TYPESAFE_PROVIDER', 'openrouter') == 'openrouter'
                 and os.getenv('TYPESAFE_BASE_URL', ENDPOINT) == ENDPOINT)
        checks['intelligence_configuration'] = 'ok' if valid else 'unavailable'
    return checks
