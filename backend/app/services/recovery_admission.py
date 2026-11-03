"""Retired simulation admission: only explicit, authenticated merchant scope."""
import os
from uuid import UUID


class AdmissionConfigurationError(ValueError):
    """A deployment setting could widen or misidentify the approved scope."""


def approved_direct_merchants():
    # Keep the existing deployment variable compatible with the rollback.
    # legacy_simulation now means terminal refusal, never simulation execution.
    if os.getenv('RECOVERY_ANALYSIS_MODE', 'legacy_simulation') != 'legacy_simulation':
        raise AdmissionConfigurationError(
            'retirement requires legacy_simulation denial default; '
            'use RECOVERY_DIRECT_MERCHANT_IDS for approved direct merchants')
    merchants = set()
    for item in os.getenv('RECOVERY_DIRECT_MERCHANT_IDS', '').split(','):
        merchant = item.strip()
        if not merchant:
            continue
        try:
            value = UUID(merchant)
        except ValueError:
            raise AdmissionConfigurationError('direct merchant allowlist requires canonical non-zero UUIDs') from None
        if str(value) != merchant or value.int == 0:
            raise AdmissionConfigurationError('direct merchant allowlist requires canonical non-zero UUIDs')
        merchants.add(merchant)
    return frozenset(merchants)
