"""Graph-lifecycle ledger for per-cart Zep scratch graphs (issue #72).

Thin Redis writer/reader for the scratch registry (SCHEMA docs/specs/issue-72):

- ``zep:graph:<graph_id>`` hash - record of truth for attribution
  (graph_kind / merchant_id / created_at, plus deleted_at / delete_source
  once the graph is gone; deleted records expire after 30 days).
- ``zep:scratch:active`` sorted set - scratch registry, secondary diagnostic
  (member = graph_id, score = creation epoch seconds).
- ``zep:merchant:<merchant_id>:graphs`` set - offboarding enumeration.

Invariant (SCHEMA §1): **correctness never depends on this ledger.** Sweep
decisions and the orphan-age metric derive from Zep's own listing; the ledger
corroborates ages, attributes graphs to merchants, and enumerates them for
offboarding. Every function takes its connection from
``job_queue.get_redis_connection()`` and no-ops with a warning when Redis is
unavailable - a Redis outage must never fail an analysis or a sweep (PRD FR-4).

The one loud failure is ``record_created`` on a non-scratch graph id: #61's
long-lived ``merchant_*`` store graphs are structurally rejected from the
scratch registry, and a mismatched id is a programming error, not a data state.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from redis.exceptions import RedisError

from .job_queue import get_redis_connection

logger = logging.getLogger("mirofish.cart_recovery")

# Scratch graph ids exactly as minted by graph_builder.create_graph
# (f"mirofish_{uuid4().hex[:16]}"). record_created asserts against this so a
# store graph can never enter the scratch registry; the #72 sweeper (W2)
# applies the same pattern to Zep's listing as its independent guard.
SWEEPABLE_RE = re.compile(r"^mirofish_[0-9a-f]{16}$")

# Deleted-graph hashes are kept 30 days for diagnostics, then expire
# (SCHEMA §2.1). Live hashes carry no TTL.
DELETED_RECORD_TTL_SECONDS = 2592000  # 30 days

_ACTIVE_KEY = "zep:scratch:active"


def _graph_key(graph_id: str) -> str:
    return f"zep:graph:{graph_id}"


def _merchant_key(merchant_id: str) -> str:
    return f"zep:merchant:{merchant_id}:graphs"


def _decode(raw) -> Optional[str]:
    # errors="replace": a corrupt (non-UTF-8) hash value must never raise a
    # UnicodeDecodeError, which would escape the RedisError guards and abort a
    # caller mid-flight (e.g. the W2 sweeper's post-delete bookkeeping, F8).
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", "replace")
    return raw


def record_created(graph_id: str, merchant_id: str) -> None:
    """Record a freshly created scratch graph (hash + active zset + merchant set).

    Raises ``ValueError`` if ``graph_id`` is not a scratch id - store graphs
    (#61) must never enter the scratch registry, and that is a programming
    error worth failing loud on. Redis unavailability, by contrast, degrades
    to a warning: the ledger is diagnostic, never load-bearing.
    """
    # fullmatch, not match: `$` alone would accept a trailing newline (the same
    # footgun paths.py guards with \A...\Z), and the W2 sweeper uses fullmatch.
    if not SWEEPABLE_RE.fullmatch(graph_id):
        raise ValueError(f"not a scratch graph id: {graph_id!r}")
    conn = get_redis_connection()
    if conn is None:
        logger.warning("graph lifecycle: Redis unavailable; skipping record_created for %s", graph_id)
        return
    created_at = int(time.time())
    try:
        pipe = conn.pipeline()
        pipe.hset(_graph_key(graph_id), mapping={
            "graph_kind": "scratch",
            "merchant_id": merchant_id,
            "created_at": str(created_at),
        })
        # A graph id can be re-minted after a prior incarnation was deleted (uuid
        # collision is astronomically unlikely, but a tombstoned hash could still
        # linger inside its 30-day TTL). Clear the tombstone fields so the new
        # incarnation starts a clean lifecycle and ``created_at_for`` corroborates
        # THIS creation, not the dead record's (F10). Also drop the 30-day
        # deleted-record TTL so the live hash has no expiry.
        pipe.hdel(_graph_key(graph_id), "deleted_at", "delete_source")
        pipe.persist(_graph_key(graph_id))
        pipe.zadd(_ACTIVE_KEY, {graph_id: created_at})
        pipe.sadd(_merchant_key(merchant_id), graph_id)
        pipe.execute()
    except RedisError:
        logger.warning("graph lifecycle: record_created failed for %s (Redis error)", graph_id)


def record_deleted(graph_id: str, source: str) -> None:
    """Close a graph's ledger record after its Zep deletion succeeded.

    ``source`` is ``"inline"`` (the analysis finally) or ``"sweep"`` (the W2
    sweeper) - the sweep share of deletions approximates the inline-failure
    rate. A graph with no prior ``record_created`` (pre-ledger orphan, or one
    created during a Redis outage) gets its hash written here with
    ``merchant_id`` absent, which is explicitly distinguishable from the
    sentinel merchant id.
    """
    conn = get_redis_connection()
    if conn is None:
        logger.warning("graph lifecycle: Redis unavailable; skipping record_deleted for %s", graph_id)
        return
    try:
        merchant_id = _decode(conn.hget(_graph_key(graph_id), "merchant_id"))
        pipe = conn.pipeline()
        pipe.hset(_graph_key(graph_id), mapping={
            "deleted_at": str(int(time.time())),
            "delete_source": source,
        })
        pipe.expire(_graph_key(graph_id), DELETED_RECORD_TTL_SECONDS)
        pipe.zrem(_ACTIVE_KEY, graph_id)
        if merchant_id is not None:
            pipe.srem(_merchant_key(merchant_id), graph_id)
        pipe.execute()
    except RedisError:
        logger.warning("graph lifecycle: record_deleted failed for %s (Redis error)", graph_id)


def created_at_for(graph_id: str) -> Optional[int]:
    """Return the ledger's creation epoch for ``graph_id``, or None.

    Corroboration read for the W2 sweeper's fail-closed eligibility rule
    (TDD §3.2): when Zep's own ``created_at`` is unparseable, a ledger age can
    still prove a graph old enough to delete. None means "no proven age here".
    """
    conn = get_redis_connection()
    if conn is None:
        logger.warning("graph lifecycle: Redis unavailable; no created_at for %s", graph_id)
        return None
    try:
        record = conn.hgetall(_graph_key(graph_id))
    except RedisError:
        logger.warning("graph lifecycle: created_at_for failed for %s (Redis error)", graph_id)
        return None
    if not record:
        return None
    record = {_decode(k): _decode(v) for k, v in record.items()}
    # F10: a TOMBSTONED record (deleted_at set) belongs to a prior, deleted
    # incarnation of this id — it must not corroborate the age of a graph the
    # sweeper is currently listing. Fail closed to unknown-age so a re-minted id
    # is never aged off a dead record and wrongly deleted.
    if record.get("deleted_at") is not None:
        logger.warning(
            "graph lifecycle: created_at for %s is on a tombstoned record; "
            "not corroborating (fail-closed)", graph_id,
        )
        return None
    raw = record.get("created_at")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("graph lifecycle: malformed created_at for %s", graph_id)
        return None


def graphs_for_merchant(merchant_id: str) -> list[str]:
    """Return the live graph ids attributed to ``merchant_id`` (offboarding
    enumeration, PRD FR-4). Empty when Redis is unavailable - the ledger
    enumerates, it never gates."""
    conn = get_redis_connection()
    if conn is None:
        logger.warning("graph lifecycle: Redis unavailable; no graph enumeration for merchant %s", merchant_id)
        return []
    try:
        members = conn.smembers(_merchant_key(merchant_id))
    except RedisError:
        logger.warning("graph lifecycle: graphs_for_merchant failed for %s (Redis error)", merchant_id)
        return []
    return sorted(_decode(m) for m in members)
