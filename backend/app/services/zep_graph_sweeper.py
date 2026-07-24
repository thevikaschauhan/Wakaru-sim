"""Orphan-graph sweeper for per-cart Zep scratch graphs (issue #72, TDD §3.2).

The inline delete in ``cart_recovery_workflow`` (W1) is a best-effort fast path;
this sweeper is the durable backstop. It lists Zep's own graphs, keeps only the
scratch ids (``^mirofish_[0-9a-f]{16}$``), proves each one's age
**fail-closed**, and deletes the ones older than the TTL — oldest first, capped
per run, from a genuinely bounded candidate heap.

Design invariants (revision 3):

- **Correctness is Zep-derived, never ledger-derived.** The listing is the work
  queue and the orphan-age metric is computed from the scan. The Redis ledger
  (``graph_lifecycle``) only *corroborates* an age when the vendor timestamp is
  unparseable and is reconciled as a diagnostic — a Redis outage degrades the
  ledger, never a deletion decision (SCHEMA §1).
- **Fail-closed eligibility.** A graph whose age cannot be proven (neither the
  vendor ``created_at`` nor the ledger) is retained, counted
  (``skipped_unknown_age``) and alerted — never auto-deleted. A vendor
  timestamp-format regression degrades to retention, not a mass deletion of
  in-flight graphs (V-3). Operator-approved disposal of persistent unknown-age
  orphans is the ``ZEP_SWEEP_FORCE_DELETE_IDS`` escape hatch (§8 runbook).
- **Bounded memory.** Only a ``max_deletes``-sized heap of the oldest eligible
  candidates plus running aggregates are retained; the full listing is streamed
  page-by-page and discarded. The heap never exceeds ``max_deletes`` entries.

The Zep client is obtained through :func:`_zep_client` so unit tests can inject a
fake without a real API key or network. The public :func:`sweep_orphan_graphs`
signature takes no client argument (TDD §3.2).
"""
from __future__ import annotations

import heapq
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import sentry_sdk
from redis.exceptions import RedisError
from zep_cloud.client import Zep
from zep_cloud.core.api_error import ApiError

from ..config import Config
from . import graph_lifecycle
from .job_queue import get_redis_connection

logger = logging.getLogger("mirofish.cart_recovery")

# One source of truth for "is this a per-cart scratch graph": reuse the ledger's
# pattern (W1) rather than redefining it. ``merchant_*`` store graphs (#61) and
# the ``merchant_<hex>_tombstone`` can never match, so they are structurally
# invisible to the sweeper (fullmatch, so a trailing newline cannot sneak in).
SWEEPABLE_RE = graph_lifecycle.SWEEPABLE_RE

# The scratch registry key the sweeper reconciles against (diagnostic only).
_ACTIVE_KEY = graph_lifecycle._ACTIVE_KEY

_SECONDS_PER_HOUR = 3600


# --------------------------------------------------------------------------
# Config — module-level funcs reading live os.environ (Config.validate style),
# with floor guards mirroring job_queue.analyze_job_timeout's footgun guard.
# --------------------------------------------------------------------------

def _int_env(name: str, default: int, floor: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value < floor:
        logger.warning(
            "%s=%d is below the floor of %d; using the default %d",
            name, value, floor, default,
        )
        return default
    return value


def sweep_ttl_hours() -> int:
    """Graph age (hours) past which a proven-old scratch graph is sweepable."""
    return _int_env("ZEP_GRAPH_TTL_HOURS", default=24, floor=6)


def sweep_interval_minutes() -> int:
    """Minutes between sweep occurrences (used by the scheduler)."""
    return _int_env("ZEP_SWEEP_INTERVAL_MINUTES", default=60, floor=5)


def sweep_page_size() -> int:
    """``graph.list_all`` page size."""
    return _int_env("ZEP_SWEEP_PAGE_SIZE", default=100, floor=1)


def sweep_max_deletes() -> int:
    """Hard cap on deletions per sweep occurrence (bounds the candidate heap)."""
    return _int_env("ZEP_SWEEP_MAX_DELETES", default=200, floor=1)


def sweep_dry_run() -> bool:
    """Whether this sweep only logs intent (True) or actually deletes (False).

    One rule (revision 2): deletion is enabled **only** when the value, after
    ``.strip().lower()``, equals ``"false"``. Absent, empty, or anything else
    (``"true"``, ``"0"``, ``"no"``, garbage) ⇒ dry run. The rollout ships the
    var unset, so the first deploy is always dry.
    """
    raw = os.environ.get("ZEP_SWEEP_DRY_RUN")
    if raw is None:
        return True
    return raw.strip().lower() != "false"


def force_delete_ids() -> list[str]:
    """Operator escape hatch: exact ids to dispose regardless of age / dry-run.

    Comma-separated exact graph ids from ``ZEP_SWEEP_FORCE_DELETE_IDS`` (§8
    unknown-age disposal). Consumed by the next sweep, deleted per-graph-guarded
    and logged loudly, then the operator unsets the var.
    """
    raw = os.environ.get("ZEP_SWEEP_FORCE_DELETE_IDS")
    if not raw:
        return []
    return [gid.strip() for gid in raw.split(",") if gid.strip()]


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------

@dataclass
class SweepStats:
    """The one summary line's fields (TDD §3.2 step 6). ``oldest_scratch_age_seconds``
    is the oldest matched graph that REMAINS after this sweep's deletions."""
    scanned: int = 0
    matched: int = 0
    eligible_total: int = 0
    deleted: int = 0
    failed: int = 0
    skipped_dry_run: int = 0
    skipped_unknown_age: int = 0
    truncated_backlog: int = 0
    oldest_scratch_age_seconds: int = 0
    total_count: int = 0
    ledger_drift: int = 0


# --------------------------------------------------------------------------
# Injection seams (monkeypatched by unit tests)
# --------------------------------------------------------------------------

def _zep_client() -> Zep:
    """Return a Zep client. Unit tests monkeypatch this to inject a fake so the
    public ``sweep_orphan_graphs`` signature never carries a client parameter."""
    return Zep(api_key=Config.ZEP_API_KEY)


def _heap_size_hook(size: int) -> None:
    """No-op instrumentation seam. Called after every bounded-heap push so a
    test can assert the memory bound (``size`` never exceeds ``max_deletes``)."""


# --------------------------------------------------------------------------
# Age proving (fail-closed)
# --------------------------------------------------------------------------

def _parse_vendor_age(created_at, now_ts: float) -> Optional[float]:
    """Age in seconds from the vendor ``created_at`` string, or None if it does
    not parse. ISO-8601 via ``datetime.fromisoformat`` after ``Z`` normalization;
    a naive timestamp is assumed UTC."""
    if not isinstance(created_at, str):
        return None
    raw = created_at.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now_ts - dt.timestamp()


def _proven_age_seconds(graph_id: str, created_at, now_ts: float) -> Optional[float]:
    """Fail-closed age: vendor timestamp, else ledger corroboration, else None
    (unknown ⇒ never auto-deleted)."""
    age = _parse_vendor_age(created_at, now_ts)
    if age is not None:
        return age
    ledger_created = graph_lifecycle.created_at_for(graph_id)
    if ledger_created is not None:
        return now_ts - ledger_created
    return None


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

def sweep_orphan_graphs(
    *, dry_run: bool, ttl_hours: int, page_size: int, max_deletes: int
) -> SweepStats:
    """Single-pass, bounded-memory sweep of orphaned per-cart scratch graphs.

    See the module docstring and TDD §3.2 for the load-bearing algorithm. A
    sweep-level exception (e.g. the listing itself failing) is captured to
    Sentry and re-raised so the occurrence's cron check-in records ``error`` and
    the ``on_failure`` chain heal fires; per-graph delete failures are guarded
    and never abort the sweep.
    """
    now_ts = time.time()
    ttl_seconds = ttl_hours * _SECONDS_PER_HOUR
    forced = set(force_delete_ids())
    stats = SweepStats()

    try:
        # Bounded candidate heap: min-heap keyed by age, so the youngest sits at
        # the top and is evicted first when the heap would exceed max_deletes —
        # leaving exactly the oldest <= max_deletes eligible candidates.
        heap: list[tuple[float, str]] = []
        # Running aggregates for the post-deletion "oldest remaining" metric.
        max_non_eligible_age = 0.0      # matched but age <= ttl
        max_evicted_eligible_age = 0.0  # eligible but pushed out of the capped heap

        # Snapshot the scratch registry once; discard ids as they are seen in the
        # listing. Whatever remains at the end is drift (in ledger, absent in Zep).
        conn = get_redis_connection()
        registry_stale = _load_registry(conn)

        client = _zep_client()

        page = 1
        while True:
            response = client.graph.list_all(page_number=page, page_size=page_size)
            graphs = getattr(response, "graphs", None)
            if not graphs:
                break

            total_count = getattr(response, "total_count", None)
            if total_count is not None:
                stats.total_count = total_count

            for graph in graphs:
                stats.scanned += 1
                graph_id = getattr(graph, "graph_id", None)
                if not isinstance(graph_id, str) or not SWEEPABLE_RE.fullmatch(graph_id):
                    continue
                stats.matched += 1
                registry_stale.discard(graph_id)

                # Force-disposal ids are handled separately and unconditionally;
                # keep them out of the age/heap machinery entirely.
                if graph_id in forced:
                    continue

                age = _proven_age_seconds(graph_id, getattr(graph, "created_at", None), now_ts)
                if age is None:
                    stats.skipped_unknown_age += 1
                    logger.warning(
                        "zep_sweep: unknown age for %s; retaining (fail-closed)", graph_id
                    )
                    continue

                if age > ttl_seconds:
                    stats.eligible_total += 1
                    heapq.heappush(heap, (age, graph_id))
                    if len(heap) > max_deletes:
                        evicted_age, _ = heapq.heappop(heap)
                        if evicted_age > max_evicted_eligible_age:
                            max_evicted_eligible_age = evicted_age
                    _heap_size_hook(len(heap))
                elif age > max_non_eligible_age:
                    max_non_eligible_age = age

            page += 1

        stats.truncated_backlog = stats.eligible_total - len(heap)

        # Delete oldest-first (the heap is a min-heap, so reverse-sort by age).
        # No deletion happened during pagination (that would shift pages).
        max_undeleted_eligible_age = 0.0
        for age, graph_id in sorted(heap, key=lambda entry: entry[0], reverse=True):
            if dry_run:
                stats.skipped_dry_run += 1
                logger.warning(
                    "zep_sweep dry-run: would delete %s (age=%ds > ttl=%dh)",
                    graph_id, int(age), ttl_hours,
                )
                if age > max_undeleted_eligible_age:
                    max_undeleted_eligible_age = age
                continue
            if _delete_graph(client, graph_id):
                stats.deleted += 1
            else:
                stats.failed += 1
                if age > max_undeleted_eligible_age:
                    max_undeleted_eligible_age = age

        # Force-disposal pass: exact ids only, regardless of age or dry-run.
        for graph_id in forced:
            logger.warning("zep_sweep: FORCE deleting %s (operator escape hatch)", graph_id)
            if _delete_graph(client, graph_id):
                stats.deleted += 1
            else:
                stats.failed += 1

        # Oldest matched graph that REMAINS after this sweep (Zep-derived metric).
        stats.oldest_scratch_age_seconds = int(max(
            max_non_eligible_age, max_evicted_eligible_age, max_undeleted_eligible_age
        ))

        # Reconcile the diagnostic registry: drop entries whose graph is gone.
        stats.ledger_drift = _reconcile_registry(conn, registry_stale)

    except Exception:
        logger.exception("zep_sweep: sweep aborted by an exception")
        sentry_sdk.capture_message("zep_sweep: sweep aborted by an exception", level="error")
        raise

    _emit_summary(stats)
    _maybe_alert(stats, ttl_hours)
    return stats


def _delete_graph(client, graph_id: str) -> bool:
    """Delete one graph, per-graph guarded. Returns True on success (incl. an
    already-gone 404, V-1), False on a real failure. Closes the ledger record on
    success so the id leaves the active/merchant registries."""
    try:
        client.graph.delete(graph_id)
    except ApiError as exc:
        status = getattr(exc, "status_code", None)
        if status == 404:
            # Already deleted (concurrent cleanup / prior sweep) — a success end
            # state, not a failure. Close the ledger so the id does not linger.
            graph_lifecycle.record_deleted(graph_id, source="sweep")
            return True
        logger.warning("zep_sweep: delete failed for %s (status=%s)", graph_id, status)
        return False
    except Exception:
        logger.warning("zep_sweep: delete failed for %s", graph_id)
        return False
    graph_lifecycle.record_deleted(graph_id, source="sweep")
    return True


def _load_registry(conn) -> set:
    """Snapshot the scratch registry members (diagnostic). Empty on a Redis
    outage — reconciliation is a diagnostic, never load-bearing."""
    if conn is None:
        return set()
    try:
        members = conn.zrange(_ACTIVE_KEY, 0, -1)
    except RedisError:
        logger.warning("zep_sweep: could not read scratch registry (Redis error)")
        return set()
    return {graph_lifecycle._decode(m) for m in members}


def _reconcile_registry(conn, stale: set) -> int:
    """Remove registry entries whose graphs no longer exist in Zep. Returns the
    drift count. Guarded — a Redis outage yields 0 drift, never an exception."""
    if conn is None or not stale:
        return 0
    try:
        conn.zrem(_ACTIVE_KEY, *stale)
    except RedisError:
        logger.warning("zep_sweep: registry reconciliation failed (Redis error)")
        return 0
    return len(stale)


def _emit_summary(stats: SweepStats) -> None:
    logger.info(
        "zep_sweep scanned=%d matched=%d eligible_total=%d deleted=%d failed=%d "
        "skipped_dry_run=%d skipped_unknown_age=%d truncated_backlog=%d "
        "oldest_scratch_age_s=%d total_count=%d ledger_drift=%d",
        stats.scanned, stats.matched, stats.eligible_total, stats.deleted,
        stats.failed, stats.skipped_dry_run, stats.skipped_unknown_age,
        stats.truncated_backlog, stats.oldest_scratch_age_seconds,
        stats.total_count, stats.ledger_drift,
    )


def _maybe_alert(stats: SweepStats, ttl_hours: int) -> None:
    reasons = []
    if stats.failed > 0:
        reasons.append(f"failed={stats.failed}")
    if stats.skipped_unknown_age > 0:
        reasons.append(f"skipped_unknown_age={stats.skipped_unknown_age}")
    if stats.oldest_scratch_age_seconds > 2 * ttl_hours * _SECONDS_PER_HOUR:
        reasons.append(
            f"oldest_scratch_age_s={stats.oldest_scratch_age_seconds} > 2x ttl"
        )
    if reasons:
        sentry_sdk.capture_message(
            "zep_sweep alert: " + "; ".join(reasons), level="warning"
        )
