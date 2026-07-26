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
- **Bounded deletion, population-bounded scan memory (revision 5, honest
  restatement of the r3 claim).** The *deletion* set is a ``max_deletes``-sized
  heap of the oldest eligible candidates — that never grows with the backlog.
  The per-scan bookkeeping (distinct-id / seen-matched / unknown-age sets and
  the active-registry snapshot) is O(distinct scratch graphs), NOT O(1): it is
  bounded by the scratch-graph population, which is small by design (SCHEMA §4 —
  dozens in steady state; the sweep exists to keep it small) and ~tens of bytes
  per id even for a large historical backlog. Dedup, drift, and listing-
  completeness genuinely need those distinct-id sets, so this is a deliberate,
  bounded cost, not the false "only a heap is retained" of r3.

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


def _sweepable_force_ids() -> set[str]:
    """Operator force ids filtered to scratch ids only (F2 hardening).

    A ``ZEP_SWEEP_FORCE_DELETE_IDS`` entry that is not a ``mirofish_<hex16>``
    scratch id (a ``merchant_*`` store graph (#61), the ``*_tombstone``, or a
    typo) is REFUSED and logged loudly, never deleted: the force pass must never
    bypass the core #72 invariant that only per-cart scratch graphs are
    sweepable.
    """
    ids: set[str] = set()
    for gid in force_delete_ids():
        if SWEEPABLE_RE.fullmatch(gid):
            ids.add(gid)
        else:
            logger.warning(
                "zep_sweep: refusing force-delete of non-scratch id %r "
                "(only mirofish_<hex16> scratch graphs are sweepable)", gid,
            )
    return ids


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
    zep_only_unattributed: int = 0
    listing_incomplete: bool = False


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
        # A ledger timestamp that is non-positive or in the future is impossible
        # (clock skew / corrupt hash) and cannot prove age. Treat it as UNKNOWN
        # so retention stays fail-closed (F11) — never as a huge positive age
        # (from e.g. ``created_at=-1``) that would make the graph deletable.
        if ledger_created <= 0 or ledger_created > now_ts:
            logger.warning(
                "zep_sweep: implausible ledger created_at=%r for %s; "
                "treating age as unknown (fail-closed)", ledger_created, graph_id,
            )
            return None
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
    forced = _sweepable_force_ids()  # F2: reject non-scratch force ids before any delete
    stats = SweepStats()

    try:
        # Bounded candidate heap: min-heap keyed by age, so the youngest sits at
        # the top and is evicted first when the heap would exceed max_deletes —
        # leaving exactly the oldest <= max_deletes eligible candidates.
        heap: list[tuple[float, str]] = []
        # Running aggregates for the post-deletion "oldest remaining" metric.
        max_non_eligible_age = 0.0      # matched but age <= ttl
        max_evicted_eligible_age = 0.0  # eligible but pushed out of the capped heap

        # Snapshot the scratch registry once. ``registry_stale`` is mutated as ids
        # are seen (whatever remains = ledger-only drift, in ledger but absent in
        # Zep). ``registry_snapshot`` is kept immutable for the F7 Zep-only check.
        conn = get_redis_connection()
        registry_snapshot = _load_registry(conn)
        registry_stale = set(registry_snapshot)

        client = _zep_client()

        # F7: dedup matched ids so a paginated repeat cannot push the same graph
        # into the heap twice (wasting a cap slot / deleting it twice) or inflate
        # per-distinct counters. F2: ids observed unknown-age here are the ONLY
        # force-disposal candidates (§8.5 runbook intent).
        seen_matched: set[str] = set()
        unknown_age_seen: set[str] = set()
        # F1: completeness is judged over DISTINCT graph ids (all ids, scratch or
        # not), never raw scanned rows — a listing that repeats rows across pages
        # must not falsely satisfy ``total_count`` and authorize destructive
        # reconciliation of a live graph's ledger.
        distinct_ids_seen: set[str] = set()

        # F1/F6: scan-completeness signals. A listing that ends before its
        # DISTINCT ids cover ``total_count``, whose ``total_count`` shifts between
        # pages, or that never reports ``total_count`` at all is UNCERTAIN —
        # reconciliation must not treat unseen registry members as stale on
        # partial data.
        total_count_seen: Optional[int] = None
        total_count_stable = True

        page = 1
        while True:
            response = client.graph.list_all(page_number=page, page_size=page_size)

            # F1: read total_count on EVERY response — BEFORE the empty-page break
            # — so an empty first page carrying a nonzero count still marks the
            # listing incomplete instead of silently wiping the whole registry.
            total_count = getattr(response, "total_count", None)
            if total_count is not None:
                if total_count_seen is not None and total_count != total_count_seen:
                    total_count_stable = False
                total_count_seen = total_count
                stats.total_count = total_count

            graphs = getattr(response, "graphs", None)
            if not graphs:
                break

            for graph in graphs:
                stats.scanned += 1
                graph_id = getattr(graph, "graph_id", None)
                if not isinstance(graph_id, str):
                    continue
                distinct_ids_seen.add(graph_id)  # F1: completeness over distinct ids
                if not SWEEPABLE_RE.fullmatch(graph_id):
                    continue
                if graph_id in seen_matched:  # F7: count/consider each graph once
                    continue
                seen_matched.add(graph_id)
                stats.matched += 1
                # F7: a matched scratch graph absent from the ledger snapshot is
                # Zep-only drift (in Zep, unattributed) — counted, not silently
                # ignored (the SCHEMA drift contract is bidirectional).
                if graph_id not in registry_snapshot:
                    stats.zep_only_unattributed += 1
                registry_stale.discard(graph_id)

                age = _proven_age_seconds(graph_id, getattr(graph, "created_at", None), now_ts)
                if age is None:
                    stats.skipped_unknown_age += 1
                    unknown_age_seen.add(graph_id)
                    logger.warning(
                        "zep_sweep: unknown age for %s; retaining (fail-closed)", graph_id
                    )
                    continue

                if age > ttl_seconds:
                    stats.eligible_total += 1
                    if dry_run:
                        # F6: log the FULL eligible inventory as it streams. The
                        # runbook's first dry sweep must show the whole backlog,
                        # not just the capped heap subset the delete pass sees.
                        stats.skipped_dry_run += 1
                        logger.warning(
                            "zep_sweep dry-run: would delete %s (age=%ds > ttl=%dh)",
                            graph_id, int(age), ttl_hours,
                        )
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

        # --- Deletion passes. In dry-run these are a PURE inventory: nothing is
        # deleted and no ledger state is mutated (F9); the full eligible backlog
        # was already logged as it streamed (F6). ---
        max_undeleted_eligible_age = 0.0
        # F5: ONE delete-attempt budget shared across the normal AND force passes,
        # so the per-occurrence hard cap can never be doubled (the force pass used
        # to start a fresh budget). Bounds real Zep delete calls per occurrence.
        delete_budget = max_deletes

        # Normal age-based pass: delete oldest-first (min-heap, so reverse-sort by
        # age). No deletion happened during pagination (that would shift pages).
        for age, graph_id in sorted(heap, key=lambda entry: entry[0], reverse=True):
            if dry_run:
                # Inventory already logged in the scan; nothing is deleted, so
                # every eligible graph remains and feeds oldest-remaining.
                if age > max_undeleted_eligible_age:
                    max_undeleted_eligible_age = age
                continue
            if delete_budget <= 0:
                # Cap reached; the rest of the backlog drains next cycle.
                if age > max_undeleted_eligible_age:
                    max_undeleted_eligible_age = age
                continue
            delete_budget -= 1
            if _delete_graph(client, graph_id):
                stats.deleted += 1
            else:
                stats.failed += 1
                if age > max_undeleted_eligible_age:
                    max_undeleted_eligible_age = age

        # Force-disposal pass (§8.5 unknown-age runbook): dispose ONLY operator-
        # listed ids that were observed THIS scan as sweepable, unknown-age
        # scratch graphs — never a store graph (already refused above), never an
        # in-flight graph with a provable age. Shares ``delete_budget`` (F5) and
        # respects dry-run (F9 — the escape hatch disposes in real-deletion mode
        # only; a dry sweep just logs intent and mutates nothing).
        for graph_id in forced:
            if graph_id not in unknown_age_seen:
                logger.warning(
                    "zep_sweep: force id %s not observed as an unknown-age scratch "
                    "graph this scan; not disposing", graph_id,
                )
                continue
            if dry_run:
                logger.warning(
                    "zep_sweep dry-run: would FORCE delete %s (operator escape hatch)",
                    graph_id,
                )
                continue
            if delete_budget <= 0:
                logger.warning(
                    "zep_sweep: delete cap (%d) reached; deferring force id %s",
                    max_deletes, graph_id,
                )
                continue
            delete_budget -= 1
            logger.warning("zep_sweep: FORCE deleting %s (operator escape hatch)", graph_id)
            if _delete_graph(client, graph_id):
                stats.deleted += 1
            else:
                stats.failed += 1

        # Oldest matched graph that REMAINS after this sweep (Zep-derived metric).
        stats.oldest_scratch_age_seconds = int(max(
            max_non_eligible_age, max_evicted_eligible_age, max_undeleted_eligible_age
        ))

        # F1/F6: only reconcile the registry against a COMPLETE listing — one
        # whose DISTINCT ids cover a stable ``total_count`` that Zep actually
        # reported. On a partial/uncertain scan (short/empty page, shifting or
        # absent total_count), unseen members may be live graphs we simply did
        # not page to; deleting their ledger records would be data loss. Skip
        # reconciliation and alert instead. F9: dry-run never reconciles either
        # (pure inventory, no ledger mutation).
        listing_complete = (
            total_count_stable
            and total_count_seen is not None
            and len(distinct_ids_seen) >= total_count_seen
        )
        if not listing_complete:
            stats.listing_incomplete = True
            logger.warning(
                "zep_sweep: listing incomplete (distinct_seen=%d total_count=%s "
                "stable=%s); skipping registry reconciliation to avoid deleting "
                "live entries",
                len(distinct_ids_seen), total_count_seen, total_count_stable,
            )
        elif not dry_run:
            # F9: close each stale entry lifecycle-aware (hash + merchant set),
            # not a bare ZREM that leaves dangling registries behind.
            stats.ledger_drift = _reconcile_registry(conn, registry_stale)

    except Exception:
        logger.exception("zep_sweep: sweep aborted by an exception")
        sentry_sdk.capture_message("zep_sweep: sweep aborted by an exception", level="error")
        raise

    _emit_summary(stats)
    _maybe_alert(stats, ttl_hours)
    return stats


def _record_deleted_guarded(graph_id: str) -> None:
    """Close the ledger record for a graph whose Zep deletion already succeeded.

    F8: bookkeeping is best-effort and must NEVER abort the sweep nor flip a real
    vendor deletion to ``failed``. ``record_deleted`` swallows ``RedisError``
    internally, but a corrupt hash value can still raise (e.g. a decode error);
    any such failure is contained here and alerted separately, leaving the Zep
    end-state successful.
    """
    try:
        graph_lifecycle.record_deleted(graph_id, source="sweep")
    except Exception:
        logger.exception(
            "zep_sweep: ledger close failed for %s after a successful Zep delete", graph_id
        )
        sentry_sdk.capture_message(
            f"zep_sweep: ledger bookkeeping failed for {graph_id} after Zep delete",
            level="warning",
        )


def _delete_graph(client, graph_id: str) -> bool:
    """Delete one graph, per-graph guarded. Returns True on success (incl. an
    already-gone 404, V-1), False on a real failure. Closes the ledger record on
    success so the id leaves the active/merchant registries — bookkeeping is
    guarded (F8) so it can never abort the sweep after a real deletion."""
    try:
        client.graph.delete(graph_id)
    except ApiError as exc:
        status = getattr(exc, "status_code", None)
        if status == 404:
            # Already deleted (concurrent cleanup / prior sweep) — a success end
            # state, not a failure. Close the ledger so the id does not linger.
            _record_deleted_guarded(graph_id)
            return True
        logger.warning("zep_sweep: delete failed for %s (status=%s)", graph_id, status)
        return False
    except Exception:
        logger.warning("zep_sweep: delete failed for %s", graph_id)
        return False
    _record_deleted_guarded(graph_id)
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
    """Close registry entries whose graphs no longer exist in Zep, lifecycle-aware.

    F9: each stale id is closed via the ledger's deletion closure
    (``record_deleted(id, source="sweep")`` — stamp ``deleted_at`` + 30-day TTL,
    ZREM from the active zset, SREM from the merchant set) so no dangling
    ``zep:graph:<id>`` hash (which would otherwise linger with no TTL) or
    ``zep:merchant:<m>:graphs`` membership is left behind. The active zset is
    bounded (SCHEMA §4 — dozens in steady state), so a full read + per-id
    closure is fine; no ZSCAN needed. Gated by the caller on a complete listing
    (F6). Returns the drift count (ledger entries absent from Zep). Guarded — a
    Redis outage / bookkeeping error never aborts the sweep."""
    if conn is None or not stale:
        return 0
    for graph_id in stale:
        try:
            graph_lifecycle.record_deleted(graph_id, source="sweep")
        except Exception:
            logger.warning("zep_sweep: reconciliation close failed for %s", graph_id)
    return len(stale)


def _emit_summary(stats: SweepStats) -> None:
    logger.info(
        "zep_sweep scanned=%d matched=%d eligible_total=%d deleted=%d failed=%d "
        "skipped_dry_run=%d skipped_unknown_age=%d truncated_backlog=%d "
        "oldest_scratch_age_s=%d total_count=%d ledger_drift=%d "
        "zep_only_unattributed=%d listing_incomplete=%s",
        stats.scanned, stats.matched, stats.eligible_total, stats.deleted,
        stats.failed, stats.skipped_dry_run, stats.skipped_unknown_age,
        stats.truncated_backlog, stats.oldest_scratch_age_seconds,
        stats.total_count, stats.ledger_drift, stats.zep_only_unattributed,
        stats.listing_incomplete,
    )


def _maybe_alert(stats: SweepStats, ttl_hours: int) -> None:
    reasons = []
    if stats.failed > 0:
        reasons.append(f"failed={stats.failed}")
    if stats.listing_incomplete:
        reasons.append("listing_incomplete (reconciliation skipped)")
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
