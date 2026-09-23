"""Content-free operational events for the existing private log collector."""

from functools import wraps
import json
import logging
import time
from datetime import datetime, timezone


def observe_judgment(function):
    @wraps(function)
    def wrapped(self, identity, request):
        started, judgment, failure = time.monotonic(), {}, False
        try:
            judgment = function(self, identity, request)
            return judgment
        except Exception:
            failure = True
            raise
        finally:
            result = judgment.get("result", {})
            attempts = [] if judgment.get("cache_hit") else result.get("attempts", [])
            workflow = identity.get("workflow")
            if workflow not in {
                "recovery_intelligence",
                "draft_claims",
                "policy_extraction",
            }:
                workflow = "other"
            event = dict(
                version="typesafe_operation_v1",
                service="wakaru",
                workflow=workflow,
                at=datetime.now(timezone.utc).isoformat(),
                status=result.get("status", "unavailable"),
                reason="runtime_failure" if failure else result.get("error_class", ""),
                cache_hit=bool(judgment.get("cache_hit")),
                latency_ms=(time.monotonic() - started) * 1000,
                attempts=len(attempts),
                cost_usd=sum(a.get("cost") or 0 for a in attempts),
                unknown_cost_attempts=sum(a.get("cost") is None for a in attempts),
                reserved_usd=len(attempts) * 0.000625,
            )
            logging.getLogger("typesafe.operations").warning(
                "typesafe_operation %s", json.dumps(event, allow_nan=False)
            )

    return wrapped
