"""Issue #12 — Redis-backed idempotency for the paid cart-recovery endpoints.

A client retry (transport error, ambiguous timeout) on the paid `/jobs` or
`/analyze` POST must NOT run a second ~$0.05 LLM pipeline. This dedups by the
caller-supplied ``Idempotency-Key`` header.

Backed by the same Redis as the job queue (issue #20). An in-process dict is
wrong here: the web runs 2 gunicorn workers + a separate RQ worker, so process-
local state cannot cohere — only a shared store dedups authoritatively.

State machine (one atomic ``SET NX`` claim per key):
    claim_or_get -> ("won", None)      caller is first; do the work, then
                                       record(value) — or release() if the work
                                       was NOT performed (a safe-to-retry reject).
                 -> ("pending", None)  another request holds the slot mid-flight.
                 -> ("replay", value)  a prior completed value; return it.

The route is responsible for the None-connection case (Redis unavailable): it
proceeds WITHOUT dedup rather than failing the request.
"""
from __future__ import annotations

IDEMPOTENCY_TTL_SECONDS = 86400  # 24h — matches the job result TTL (issue #20).

# Marker stored while the first caller is doing the work, before record().
_PENDING = "__pending__"


def _redis_key(scope: str, idempotency_key: str) -> str:
    return f"idem:{scope}:{idempotency_key}"


def _decode(raw) -> str | None:
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode()
    return raw


def claim_or_get(connection, scope: str, idempotency_key: str):
    """Atomically claim the idempotency slot or return the prior outcome.

    Returns ``(state, value)`` — see the module docstring. ``connection`` must be
    a live Redis connection (the route handles the unavailable case).
    """
    key = _redis_key(scope, idempotency_key)
    # SET NX is the atomic gate: exactly one concurrent caller wins the claim.
    won = connection.set(key, _PENDING, nx=True, ex=IDEMPOTENCY_TTL_SECONDS)
    if won:
        return ("won", None)
    value = _decode(connection.get(key))
    if value is None or value == _PENDING:
        # The slot is claimed but no final value recorded yet (concurrent
        # in-flight). A None here would mean the key expired between SET and GET,
        # which cannot happen within a request given the 24h TTL.
        return ("pending", None)
    return ("replay", value)


def record(connection, scope: str, idempotency_key: str, value: str) -> None:
    """Store the final value (overwriting the PENDING marker) with a 24h TTL."""
    connection.set(_redis_key(scope, idempotency_key), value, ex=IDEMPOTENCY_TTL_SECONDS)


def release(connection, scope: str, idempotency_key: str) -> None:
    """Free the slot — ONLY when the work was not performed (a positive,
    safe-to-retry rejection, e.g. the queue was unavailable so no job was
    created). Never release on an ambiguous outcome where the paid work may have
    happened (that would let a retry double-charge)."""
    connection.delete(_redis_key(scope, idempotency_key))
