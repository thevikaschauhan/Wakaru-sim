"""Issue #72 (W2) - orphan-graph sweeper unit tests (TDD §6 items 3-11).

Fake Zep client injected through ``zep_graph_sweeper._zep_client``; the Redis
ledger is fakeredis (the suite convention, see test_graph_lifecycle.py). These
cover the sweep algorithm in isolation - the lock/scheduler mechanics that need
a real RQ Worker + real Redis live in test_sweep_scheduling.py (fakeredis cannot
run a Worker and lacks Lua ``eval``, so they cannot be faked here).

Each test asserts the PROPERTY (which graphs were deleted, the memory bound, the
metric source), not merely that a method was called.
"""
import time
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest
from zep_cloud.core.api_error import ApiError

import app.services.graph_lifecycle as gl
import app.services.maintenance_queue as mq
import app.services.zep_graph_sweeper as sweeper


# --------------------------------------------------------------------------
# Fixtures + fakes
# --------------------------------------------------------------------------

@pytest.fixture
def conn(monkeypatch):
    """A shared fakeredis wired into both the sweeper and the ledger it reads."""
    c = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(gl, "get_redis_connection", lambda: c)
    monkeypatch.setattr(sweeper, "get_redis_connection", lambda: c)
    return c


def scratch_id(n: int) -> str:
    """A structurally valid per-cart scratch id (``mirofish_`` + 16 hex)."""
    return f"mirofish_{n:016x}"


def iso_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class FakeGraph:
    def __init__(self, graph_id, created_at):
        self.graph_id = graph_id
        self.created_at = created_at


class FakeListResponse:
    def __init__(self, graphs, total_count=None):
        self.graphs = graphs
        self.total_count = total_count
        self.row_count = len(graphs) if graphs else 0


class FakeGraphApi:
    """Records the interleaving of list/delete/get calls so tests can assert the
    no-delete-during-pagination rule, oldest-first deletion order, and the F1
    authoritative per-graph reconciliation confirm.

    ``get(graph_id)`` models Zep's ``graph.get``: an id present in ANY page
    "exists" (returns a stub Graph); an id absent from every page 404s (genuinely
    gone) — so a stale ledger id the listing never returns 404s by default and a
    reconciliation close still happens. Two knobs override this per id:
    ``get_exists_ids`` forces "still exists" (simulating a dropped-page listing
    gap / under-reported total_count), and ``get_error_ids`` forces a non-404
    ApiError (an uncertain confirm)."""

    def __init__(self, pages, fail_ids=None, total_count=None,
                 get_exists_ids=None, get_error_ids=None):
        self._pages = pages
        self.fail_ids = set(fail_ids or [])
        self.total_count = total_count
        self._present_ids = {
            getattr(g, "graph_id", None) for page in pages for g in page
        }
        self._present_ids.discard(None)
        self._get_exists_ids = set(get_exists_ids or [])
        self._get_error_ids = set(get_error_ids or [])
        self.events = []   # ("list", page) | ("delete", id) | ("get", id)
        self.deleted = []

    def list_all(self, page_number, page_size):
        self.events.append(("list", page_number))
        idx = page_number - 1
        graphs = self._pages[idx] if 0 <= idx < len(self._pages) else []
        return FakeListResponse(graphs, total_count=self.total_count)

    def get(self, graph_id):
        self.events.append(("get", graph_id))
        if graph_id in self._get_error_ids:
            raise ApiError(status_code=500, body="get boom")
        if graph_id in self._get_exists_ids or graph_id in self._present_ids:
            return FakeGraph(graph_id, None)
        raise ApiError(status_code=404, body="graph gone")

    def delete(self, graph_id):
        self.events.append(("delete", graph_id))
        if graph_id in self.fail_ids:
            raise RuntimeError(f"delete boom for {graph_id}")
        self.deleted.append(graph_id)


class FakeZep:
    def __init__(self, graph_api):
        self.graph = graph_api


def install_client(monkeypatch, pages, fail_ids=None, total_count=None,
                   get_exists_ids=None, get_error_ids=None):
    api = FakeGraphApi(
        pages, fail_ids=fail_ids, total_count=total_count,
        get_exists_ids=get_exists_ids, get_error_ids=get_error_ids,
    )
    monkeypatch.setattr(sweeper, "_zep_client", lambda: FakeZep(api))
    return api


@pytest.fixture
def captured_messages(monkeypatch):
    """Record sentry_sdk.capture_message calls (message, level)."""
    calls = []
    monkeypatch.setattr(
        sweeper.sentry_sdk, "capture_message",
        lambda msg, level=None: calls.append((msg, level)),
    )
    return calls


# --------------------------------------------------------------------------
# 3 - filters
# --------------------------------------------------------------------------

def test_sweeper_filters(conn, monkeypatch, captured_messages):
    old = scratch_id(1)                      # 48h -> deleted
    young = scratch_id(2)                     # 1h  -> kept
    merchant = "merchant_0123456789abcdef"    # store graph (#61) -> untouched
    shouting = "mirofish_ABCDEF0123456789"    # 16 chars, wrong case -> untouched
    pages = [[
        FakeGraph(old, iso_ago(48)),
        FakeGraph(young, iso_ago(1)),
        FakeGraph(merchant, iso_ago(48)),
        FakeGraph(shouting, iso_ago(48)),
    ]]
    api = install_client(monkeypatch, pages)

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert api.deleted == [old]              # only the proven-old scratch graph
    assert stats.scanned == 4
    assert stats.matched == 2                # merchant_ and SHOUTING never match
    assert stats.eligible_total == 1
    assert stats.deleted == 1


# --------------------------------------------------------------------------
# 4 - fail-closed unknown age
# --------------------------------------------------------------------------

def test_sweeper_unknown_age_no_ledger_is_kept_counted_and_alerts(
    conn, monkeypatch, captured_messages
):
    gid = scratch_id(10)
    pages = [[FakeGraph(gid, "not-a-timestamp")]]   # unparseable vendor ts
    api = install_client(monkeypatch, pages)

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert api.deleted == []                          # never auto-deleted
    assert stats.skipped_unknown_age == 1
    assert stats.eligible_total == 0
    # would-alert: an unknown-age graph raises a Sentry message.
    assert any("skipped_unknown_age" in msg for msg, _ in captured_messages)


def test_sweeper_unknown_vendor_age_deleted_via_ledger_corroboration(
    conn, monkeypatch, captured_messages
):
    gid = scratch_id(11)
    # Ledger proves it older than the TTL (vendor timestamp is absent).
    conn.hset(f"zep:graph:{gid}", "created_at", str(int(time.time()) - 48 * 3600))
    pages = [[FakeGraph(gid, None)]]
    api = install_client(monkeypatch, pages)

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert api.deleted == [gid]                       # corroboration path
    assert stats.skipped_unknown_age == 0
    assert stats.deleted == 1


# --------------------------------------------------------------------------
# 5 - dry-run parser matrix (only the `false` variants delete)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,should_delete", [
    (None, False), ("", False), ("true", False), ("TRUE", False),
    ("false", True), ("False", True), (" false ", True),
    ("0", False), ("no", False), ("garbage", False),
])
def test_sweeper_dry_run_parser(conn, monkeypatch, captured_messages, value, should_delete):
    if value is None:
        monkeypatch.delenv("ZEP_SWEEP_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("ZEP_SWEEP_DRY_RUN", value)
    gid = scratch_id(20)
    api = install_client(monkeypatch, [[FakeGraph(gid, iso_ago(48))]])

    stats = sweeper.sweep_orphan_graphs(
        dry_run=sweeper.sweep_dry_run(), ttl_hours=24, page_size=100, max_deletes=200
    )

    if should_delete:
        assert api.deleted == [gid]
        assert stats.skipped_dry_run == 0
    else:
        assert api.deleted == []
        assert stats.skipped_dry_run == 1


# --------------------------------------------------------------------------
# 6 - pagination + delete-after-list ordering
# --------------------------------------------------------------------------

def test_sweeper_pagination_deletes_after_full_listing_oldest_first(
    conn, monkeypatch, captured_messages
):
    g1, g2, g3 = scratch_id(31), scratch_id(32), scratch_id(33)
    pages = [
        [FakeGraph(g1, iso_ago(72)), FakeGraph(g2, iso_ago(48))],
        [FakeGraph(g3, iso_ago(36))],
    ]
    api = install_client(monkeypatch, pages)

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=2, max_deletes=200
    )

    list_positions = [i for i, e in enumerate(api.events) if e[0] == "list"]
    delete_positions = [i for i, e in enumerate(api.events) if e[0] == "delete"]
    # Every listing call precedes every delete - no deletion during pagination.
    assert max(list_positions) < min(delete_positions)
    # Oldest-first.
    assert api.deleted == [g1, g2, g3]
    assert stats.deleted == 3


# --------------------------------------------------------------------------
# 7 - partial failure
# --------------------------------------------------------------------------

def test_sweeper_partial_failure_continues_and_counts(
    conn, monkeypatch, captured_messages
):
    g1, g2, g3 = scratch_id(41), scratch_id(42), scratch_id(43)
    pages = [[
        FakeGraph(g1, iso_ago(72)),
        FakeGraph(g2, iso_ago(60)),
        FakeGraph(g3, iso_ago(48)),
    ]]
    api = install_client(monkeypatch, pages, fail_ids=[g2])

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert stats.failed == 1
    assert stats.deleted == 2
    assert g1 in api.deleted and g3 in api.deleted
    assert g2 not in api.deleted            # its delete raised, sweep continued


# --------------------------------------------------------------------------
# 8 - delete cap + bounded heap
# --------------------------------------------------------------------------

def test_sweeper_delete_cap_and_bounded_heap(conn, monkeypatch, captured_messages):
    heap_sizes = []
    monkeypatch.setattr(sweeper, "_heap_size_hook", lambda n: heap_sizes.append(n))

    ages = [90, 80, 70, 60, 50]             # all eligible (> 24h)
    by_age = {}
    page_one, page_two = [], []
    for i, hrs in enumerate(ages):
        gid = scratch_id(50 + i)
        by_age[hrs] = gid
        (page_one if i < 3 else page_two).append(FakeGraph(gid, iso_ago(hrs)))
    api = install_client(monkeypatch, [page_one, page_two])

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=3, max_deletes=2
    )

    assert stats.eligible_total == 5
    assert stats.deleted == 2
    assert stats.truncated_backlog == 3     # eligible_total - cap
    assert api.deleted == [by_age[90], by_age[80]]   # the two oldest, oldest first
    # Memory bound: the heap NEVER exceeded max_deletes across the multi-page scan.
    assert heap_sizes, "the bounded-heap hook must have fired"
    assert max(heap_sizes) <= 2


# --------------------------------------------------------------------------
# 9 - metrics from the scan, not Redis
# --------------------------------------------------------------------------

def test_metrics_oldest_age_from_scan_not_redis(conn, monkeypatch, captured_messages):
    gid = scratch_id(60)
    api = install_client(monkeypatch, [[FakeGraph(gid, iso_ago(100))]])
    # The registry claims it is brand new; the metric must ignore that and use
    # the 100h age from the Zep listing.
    conn.zadd(sweeper._ACTIVE_KEY, {gid: int(time.time())})

    stats = sweeper.sweep_orphan_graphs(
        dry_run=True, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert stats.oldest_scratch_age_seconds >= 99 * 3600


def test_metrics_registry_reconciliation_removes_stale(
    conn, monkeypatch, captured_messages
):
    live = scratch_id(70)                   # in the listing
    stale = scratch_id(71)                   # in the registry, gone from Zep
    # F1: `stale` is absent from the listing, so reconciliation confirms it via
    # graph.get, which 404s (not in any page) -> genuinely gone -> closed. No
    # total_count / listing-completeness inference is involved.
    api = install_client(monkeypatch, [[FakeGraph(live, iso_ago(1))]])
    now = int(time.time())
    conn.zadd(sweeper._ACTIVE_KEY, {live: now, stale: now})

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert stats.ledger_drift == 1
    assert conn.zscore(sweeper._ACTIVE_KEY, stale) is None   # reconciled away
    assert conn.zscore(sweeper._ACTIVE_KEY, live) is not None  # still live


# --------------------------------------------------------------------------
# 10 - ttl floor guard
# --------------------------------------------------------------------------

def test_ttl_floor(monkeypatch):
    monkeypatch.delenv("ZEP_GRAPH_TTL_HOURS", raising=False)
    assert sweeper.sweep_ttl_hours() == 24            # default
    monkeypatch.setenv("ZEP_GRAPH_TTL_HOURS", "3")
    assert sweeper.sweep_ttl_hours() == 24            # below floor -> default
    monkeypatch.setenv("ZEP_GRAPH_TTL_HOURS", "5")
    assert sweeper.sweep_ttl_hours() == 24            # below floor (6) -> default
    monkeypatch.setenv("ZEP_GRAPH_TTL_HOURS", "6")
    assert sweeper.sweep_ttl_hours() == 6             # exact floor honoured
    monkeypatch.setenv("ZEP_GRAPH_TTL_HOURS", "48")
    assert sweeper.sweep_ttl_hours() == 48            # above floor honoured
    monkeypatch.setenv("ZEP_GRAPH_TTL_HOURS", "abc")
    assert sweeper.sweep_ttl_hours() == 24            # non-integer -> default


# --------------------------------------------------------------------------
# 11 - cron check-in emitted (the occurrence body wraps the sweep)
# --------------------------------------------------------------------------

class CheckinRecorder:
    def __init__(self):
        self.calls = []   # (status, check_in_id)

    def __call__(self, *, monitor_slug, check_in_id=None, status=None,
                 duration=None, monitor_config=None):
        self.calls.append((status, check_in_id))
        return check_in_id or "test-check-in-id"


def test_cron_checkin_in_progress_then_ok_on_success(monkeypatch):
    rec = CheckinRecorder()
    monkeypatch.setattr(mq, "capture_checkin", rec)
    monkeypatch.setattr(mq, "sweep_orphan_graphs", lambda **kw: sweeper.SweepStats())

    mq._run_sweep_with_checkin()

    assert [s for s, _ in rec.calls] == [mq.MonitorStatus.IN_PROGRESS, mq.MonitorStatus.OK]
    # The OK check-in reuses the id returned by the IN_PROGRESS check-in.
    assert rec.calls[1][1] == "test-check-in-id"


def test_cron_checkin_in_progress_then_error_on_raise(monkeypatch):
    rec = CheckinRecorder()
    monkeypatch.setattr(mq, "capture_checkin", rec)

    def boom(**kw):
        raise RuntimeError("sweep boom")

    monkeypatch.setattr(mq, "sweep_orphan_graphs", boom)

    with pytest.raises(RuntimeError):
        mq._run_sweep_with_checkin()

    assert [s for s, _ in rec.calls] == [mq.MonitorStatus.IN_PROGRESS, mq.MonitorStatus.ERROR]
    assert rec.calls[1][1] == "test-check-in-id"


# ==========================================================================
# Codex review fixes (PR #78): F2, F6, F7, F8, F9, F11
# ==========================================================================

# --------------------------------------------------------------------------
# F2 - force-delete must enforce SWEEPABLE_RE + cap; never erase store graphs
# --------------------------------------------------------------------------

def test_force_delete_refuses_non_scratch_id(conn, monkeypatch, captured_messages):
    """A ``merchant_*`` store graph (#61) named in the force env var is REFUSED,
    never deleted - the core #72 invariant holds even for the escape hatch."""
    merchant = "merchant_0123456789abcdef"     # #61 store graph, never sweepable
    monkeypatch.setenv("ZEP_SWEEP_FORCE_DELETE_IDS", merchant)
    # Present in the listing and ancient - the old force pass would delete it.
    api = install_client(monkeypatch, [[FakeGraph(merchant, iso_ago(9999))]])

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert api.deleted == []                    # the store graph is NEVER deleted
    assert merchant not in api.deleted
    assert stats.matched == 0                   # SWEEPABLE_RE excludes merchant_


def test_force_delete_disposes_observed_unknown_age_scratch(
    conn, monkeypatch, captured_messages
):
    """The legitimate escape hatch (§8.5): an unknown-age scratch graph named in
    the env var IS disposed in real-deletion mode. (F9: dry-run is a pure
    inventory now — the escape hatch disposes only when dry_run is off, which is
    exactly the §8.5 rollout state; see test_force_delete_respects_dry_run.)"""
    gid = scratch_id(90)
    monkeypatch.setenv("ZEP_SWEEP_FORCE_DELETE_IDS", gid)
    # Unknown age: unparseable vendor ts, no ledger -> normally fail-closed.
    api = install_client(monkeypatch, [[FakeGraph(gid, "not-a-timestamp")]])

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert api.deleted == [gid]                 # force disposed in real mode
    assert stats.deleted == 1


def test_force_delete_bounded_by_cap(conn, monkeypatch, captured_messages):
    """The force pass is bounded by ``max_deletes`` - a stale/oversized env var
    can never trigger an unbounded delete storm."""
    ids = [scratch_id(100 + i) for i in range(3)]
    monkeypatch.setenv("ZEP_SWEEP_FORCE_DELETE_IDS", ",".join(ids))
    # All three observed unknown-age this scan (force-eligible), cap is 2.
    api = install_client(monkeypatch, [[FakeGraph(g, "garbage") for g in ids]])

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=2
    )

    assert len(api.deleted) == 2                # capped, not all three
    assert stats.deleted == 2


# --------------------------------------------------------------------------
# F1/F6 - reconciliation is now authoritative per-graph (graph.get), NOT a
# listing-completeness inference. The r3 completeness-gate tests
# (test_incomplete_listing_skips_reconciliation_and_alerts,
# test_duplicate_rows_do_not_falsely_satisfy_total_count,
# test_empty_first_page_with_nonzero_count_is_incomplete) tested the removed
# ``listing_incomplete`` gate and are superseded by the r3 reconciliation tests
# further below (test_reconcile_* and test_sweep_memory_*).
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# F7 - duplicate paginated ids must not waste the cap / double-delete
# --------------------------------------------------------------------------

def test_duplicate_paginated_ids_dedup_and_dont_waste_cap(
    conn, monkeypatch, captured_messages
):
    """A listing that repeats an id (g1, g2, g1) deletes each DISTINCT graph
    once and does not let the duplicate evict a real graph from the capped heap."""
    g1, g2 = scratch_id(110), scratch_id(111)
    pages = [
        [FakeGraph(g1, iso_ago(72)), FakeGraph(g2, iso_ago(48))],
        [FakeGraph(g1, iso_ago(72))],           # g1 repeats on page 2
    ]
    api = install_client(monkeypatch, pages)

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=2, max_deletes=2
    )

    assert api.deleted == [g1, g2]              # each distinct graph, oldest first
    assert api.deleted.count(g1) == 1           # NOT deleted twice
    assert stats.matched == 2                   # distinct, not 3
    assert stats.eligible_total == 2


# --------------------------------------------------------------------------
# F8 - bookkeeping error must not abort a successful vendor deletion
# --------------------------------------------------------------------------

def test_record_deleted_failure_does_not_abort_sweep(
    conn, monkeypatch, captured_messages
):
    """A ``record_deleted`` raising AFTER a real Zep delete must be contained -
    the sweep completes and the vendor deletion stays a success, not ``failed``."""
    gid = scratch_id(80)                        # 48h -> eligible, gets deleted
    api = install_client(monkeypatch, [[FakeGraph(gid, iso_ago(48))]])

    def boom(graph_id, source):
        raise RuntimeError("ledger boom")
    monkeypatch.setattr(gl, "record_deleted", boom)

    try:
        stats = sweeper.sweep_orphan_graphs(
            dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
        )
    except Exception as exc:  # pragma: no cover - only on an F8 regression
        pytest.fail(f"F8 regression: bookkeeping error aborted the sweep: {exc!r}")

    assert api.deleted == [gid]                 # the vendor deletion happened
    assert stats.deleted == 1                   # and stayed a success
    assert stats.failed == 0                    # never flipped to failed


def test_decode_tolerates_invalid_utf8():
    """``graph_lifecycle._decode`` must not raise ``UnicodeDecodeError`` on a
    corrupt hash value (which would escape the RedisError guards, F8)."""
    try:
        result = gl._decode(b"\xff\xfe\xfd")    # not valid UTF-8
    except UnicodeDecodeError as exc:  # pragma: no cover - only on an F8 regression
        pytest.fail(f"F8 regression: _decode raised on invalid UTF-8: {exc!r}")
    assert isinstance(result, str)              # decoded with replacement, not raised


# --------------------------------------------------------------------------
# F9 - reconciliation must be lifecycle-aware (close hash + merchant set)
# --------------------------------------------------------------------------

def test_reconciliation_closes_hash_and_merchant_set(
    conn, monkeypatch, captured_messages
):
    """A stale registry entry is closed via the ledger's deletion closure -
    stamping deleted_at, dropping the merchant-set membership and the active
    zset entry - not a bare ZREM that leaves the hash / merchant set dangling."""
    live = scratch_id(130)
    stale = scratch_id(131)
    merchant = "11111111-1111-1111-1111-111111111111"
    gl.record_created(live, merchant)           # full ledger records for both
    gl.record_created(stale, merchant)
    # Zep lists only `live`; `stale` is absent from the listing and its
    # graph.get 404s (F1: authoritatively confirmed gone) -> drift, closed
    # lifecycle-aware.
    api = install_client(monkeypatch, [[FakeGraph(live, iso_ago(1))]])

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert stats.ledger_drift == 1
    # Active zset: stale removed, live kept.
    assert conn.zscore(sweeper._ACTIVE_KEY, stale) is None
    assert conn.zscore(sweeper._ACTIVE_KEY, live) is not None
    # F9: merchant-set membership closed for stale, live retained.
    assert not conn.sismember(f"zep:merchant:{merchant}:graphs", stale)
    assert conn.sismember(f"zep:merchant:{merchant}:graphs", live)
    # F9: hash closed lifecycle-aware (deleted_at + delete_source), not dangling.
    assert conn.hget(f"zep:graph:{stale}", "deleted_at") is not None
    assert gl._decode(conn.hget(f"zep:graph:{stale}", "delete_source")) == "sweep"


# --------------------------------------------------------------------------
# F11 - impossible ledger timestamps must fail closed (unknown age)
# --------------------------------------------------------------------------

def test_ledger_created_at_nonpositive_is_unknown_age(
    conn, monkeypatch, captured_messages
):
    """A ledger ``created_at`` of -1 must NOT compute a huge positive age that
    makes the graph deletable; it is treated as unknown age (fail-closed)."""
    gid = scratch_id(140)
    conn.hset(f"zep:graph:{gid}", "created_at", "-1")   # impossible timestamp
    api = install_client(monkeypatch, [[FakeGraph(gid, None)]])  # vendor absent

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert api.deleted == []                    # kept, never deleted
    assert stats.skipped_unknown_age == 1
    assert stats.deleted == 0


def test_ledger_created_at_future_is_unknown_age(
    conn, monkeypatch, captured_messages
):
    """A future ledger ``created_at`` is impossible (clock skew / corruption) and
    is treated as unknown age (counted + retained), not a young graph."""
    gid = scratch_id(141)
    conn.hset(f"zep:graph:{gid}", "created_at", str(int(time.time()) + 10_000))
    api = install_client(monkeypatch, [[FakeGraph(gid, None)]])

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert api.deleted == []
    assert stats.skipped_unknown_age == 1       # unknown, not silently "young"


# ==========================================================================
# Re-review fixes (PR #78, second round): F1, F5, F6, F7, F9, F10, F11
# ==========================================================================

# --------------------------------------------------------------------------
# F1 - reconciliation is authoritative per-graph. The r2 completeness-gate F1
# tests moved into the r3 block at the end of this file
# (test_reconcile_* + test_sweep_memory_independent_of_nonscratch_population),
# which pin the graph.get confirmation directly.
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# F5 - the hard delete cap is shared across normal + force passes (not doubled)
# --------------------------------------------------------------------------

def test_shared_delete_budget_across_normal_and_force_pass(
    conn, monkeypatch, captured_messages
):
    """A MIXED occurrence (heap-eligible + force ids) must perform AT MOST
    ``max_deletes`` real deletes total - the force pass no longer starts a fresh
    budget after the normal pass consumed the cap."""
    old1, old2 = scratch_id(220), scratch_id(221)         # eligible (old), heap
    f1, f2 = scratch_id(222), scratch_id(223)             # unknown-age force ids
    monkeypatch.setenv("ZEP_SWEEP_FORCE_DELETE_IDS", f"{f1},{f2}")
    pages = [[
        FakeGraph(old1, iso_ago(72)), FakeGraph(old2, iso_ago(48)),
        FakeGraph(f1, "garbage"), FakeGraph(f2, "garbage"),
    ]]
    api = install_client(monkeypatch, pages, total_count=4)

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=2
    )

    assert len(api.deleted) == 2                 # cap 2 shared, NOT 4
    assert stats.deleted == 2


# --------------------------------------------------------------------------
# F6 - dry-run logs the FULL eligible inventory, not just the capped heap
# --------------------------------------------------------------------------

def test_dry_run_logs_full_eligible_inventory(conn, monkeypatch, captured_messages):
    """The runbook's first dry sweep must log EVERY eligible id (the whole
    backlog), even when it exceeds the per-run cap."""
    eligible = [scratch_id(230 + i) for i in range(5)]
    pages = [[FakeGraph(g, iso_ago(48 + i)) for i, g in enumerate(eligible)]]
    install_client(monkeypatch, pages, total_count=5)

    logged = []
    real_warning = sweeper.logger.warning
    def capture(msg, *args, **kw):
        logged.append(msg % args if args else msg)
        return real_warning(msg, *args, **kw)
    monkeypatch.setattr(sweeper.logger, "warning", capture)

    stats = sweeper.sweep_orphan_graphs(
        dry_run=True, ttl_hours=24, page_size=100, max_deletes=2
    )

    would_delete = [m for m in logged if "would delete" in m]
    assert len(would_delete) == 5                # all 5, not the capped 2
    assert stats.skipped_dry_run == 5
    for g in eligible:
        assert any(g in m for m in would_delete)


# --------------------------------------------------------------------------
# F7 - Zep-only (unattributed) drift is counted (bidirectional contract)
# --------------------------------------------------------------------------

def test_zep_only_graph_counted_as_drift(conn, monkeypatch, captured_messages):
    """A matched scratch graph present in Zep but absent from the ledger snapshot
    is Zep-only drift - counted, not silently ignored."""
    attributed = scratch_id(240)
    unattributed = scratch_id(241)               # in Zep, never in the ledger
    now = int(time.time())
    conn.zadd(sweeper._ACTIVE_KEY, {attributed: now})   # only `attributed` known
    api = install_client(
        monkeypatch,
        [[FakeGraph(attributed, iso_ago(1)), FakeGraph(unattributed, iso_ago(1))]],
        total_count=2,
    )

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert stats.zep_only_unattributed == 1      # `unattributed` counted
    assert stats.matched == 2


# --------------------------------------------------------------------------
# F9 - dry-run must not mutate lifecycle state (pure inventory)
# --------------------------------------------------------------------------

def test_dry_run_does_not_mutate_ledger(conn, monkeypatch, captured_messages):
    """Reviewer repro: an empty listing under dry_run left an active ledger entry
    removed + tombstoned. Dry-run must reconcile NOTHING - and (F1/F9) must not
    even CONSULT graph.get to confirm a stale id, since it mutates nothing."""
    stale = scratch_id(250)
    gl.record_created(stale, "11111111-1111-1111-1111-111111111111")
    api = install_client(monkeypatch, [[]], total_count=0)     # empty listing

    stats = sweeper.sweep_orphan_graphs(
        dry_run=True, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert stats.ledger_drift == 0                                    # no reconcile
    assert stats.reconcile_unconfirmed == 0                           # no confirm pass
    assert conn.zscore(sweeper._ACTIVE_KEY, stale) is not None        # entry intact
    assert conn.hget(f"zep:graph:{stale}", "deleted_at") is None      # not tombstoned
    # F9: dry-run is a pure inventory - no per-graph confirm was issued.
    assert not any(e[0] == "get" for e in api.events)


# --------------------------------------------------------------------------
# F10 - a tombstoned ledger record must not corroborate a new incarnation
# --------------------------------------------------------------------------

def test_tombstoned_ledger_does_not_corroborate_new_graph(
    conn, monkeypatch, captured_messages
):
    """A listed graph with NO vendor time and only a 48h-old TOMBSTONED ledger
    hash (deleted_at set) must be RETAINED (unknown age), not aged off the dead
    record and deleted."""
    gid = scratch_id(260)
    old = int(time.time()) - 48 * 3600
    # A tombstoned record: created 48h ago, then deleted (deleted_at present).
    conn.hset(f"zep:graph:{gid}", mapping={
        "graph_kind": "scratch", "created_at": str(old),
        "deleted_at": str(old + 60), "delete_source": "sweep",
    })
    api = install_client(monkeypatch, [[FakeGraph(gid, None)]])  # no vendor time

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert api.deleted == []                     # NOT deleted off the dead record
    assert stats.skipped_unknown_age == 1        # fail-closed unknown age
    assert stats.deleted == 0


def test_record_created_clears_tombstone_on_reincarnation(conn):
    """Re-minting an id must clear deleted_at/delete_source so created_at_for
    corroborates the NEW incarnation, not the dead one."""
    gid = scratch_id(261)
    old = int(time.time()) - 48 * 3600
    conn.hset(f"zep:graph:{gid}", mapping={
        "graph_kind": "scratch", "created_at": str(old),
        "deleted_at": str(old + 60), "delete_source": "sweep",
    })
    assert gl.created_at_for(gid) is None        # tombstoned -> no corroboration

    gl.record_created(gid, "11111111-1111-1111-1111-111111111111")
    assert conn.hget(f"zep:graph:{gid}", "deleted_at") is None   # cleared
    fresh = gl.created_at_for(gid)
    assert fresh is not None and fresh >= old    # corroborates the new incarnation


# --------------------------------------------------------------------------
# F11 - the 404-as-success branch is pinned
# --------------------------------------------------------------------------

def test_delete_404_counts_success_and_closes_ledger(
    conn, monkeypatch, captured_messages
):
    """A Zep delete raising ApiError(status_code=404) is idempotent success
    (V-1): counted as deleted, ledger closed, NOT failed."""
    from zep_cloud.core.api_error import ApiError

    gid = scratch_id(270)
    gl.record_created(gid, "11111111-1111-1111-1111-111111111111")

    class ApiErr404Api(FakeGraphApi):
        def delete(self, graph_id):
            self.events.append(("delete", graph_id))
            raise ApiError(status_code=404, body="already gone")

    api = ApiErr404Api([[FakeGraph(gid, iso_ago(48))]], total_count=1)
    monkeypatch.setattr(sweeper, "_zep_client", lambda: FakeZep(api))

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert stats.deleted == 1                    # 404 counts as success
    assert stats.failed == 0                     # NOT failed
    assert conn.hget(f"zep:graph:{gid}", "deleted_at") is not None   # ledger closed


def test_delete_non_404_apierror_counts_failed(conn, monkeypatch, captured_messages):
    """A non-404 ApiError is a real failure - counted as failed, not success."""
    from zep_cloud.core.api_error import ApiError

    gid = scratch_id(271)

    class ApiErr500Api(FakeGraphApi):
        def delete(self, graph_id):
            self.events.append(("delete", graph_id))
            raise ApiError(status_code=500, body="server error")

    api = ApiErr500Api([[FakeGraph(gid, iso_ago(48))]], total_count=1)
    monkeypatch.setattr(sweeper, "_zep_client", lambda: FakeZep(api))

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert stats.deleted == 0
    assert stats.failed == 1                     # real failure


# ==========================================================================
# Third-round external review fixes (PR #78): F1 + F6 reconciliation redesign.
# Reconciliation confirms each stale ledger id GONE via a per-graph graph.get
# 404 before closing it - it never infers "gone" from listing completeness, and
# it retains no O(all-listed-ids) structure. These supersede the r2
# completeness-gate tests (listing_incomplete / distinct_ids_seen), now removed.
# ==========================================================================

def test_reconcile_confirms_gone_before_closing(
    conn, monkeypatch, captured_messages
):
    """A ledger entry absent from the listing whose graph.get 404s is
    AUTHORITATIVELY confirmed gone and IS closed: ledger_drift == 1, hash
    tombstoned, and the confirm (graph.get) actually happened."""
    live = scratch_id(320)                        # in the listing, young, kept
    gone = scratch_id(321)                         # in the ledger, 404s on get
    merchant = "11111111-1111-1111-1111-111111111111"
    gl.record_created(live, merchant)
    gl.record_created(gone, merchant)
    api = install_client(monkeypatch, [[FakeGraph(live, iso_ago(1))]])

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert ("get", gone) in api.events            # authoritatively confirmed
    assert stats.ledger_drift == 1
    assert stats.reconcile_unconfirmed == 0
    assert conn.zscore(sweeper._ACTIVE_KEY, gone) is None             # closed
    assert conn.hget(f"zep:graph:{gone}", "deleted_at") is not None   # tombstoned
    assert conn.zscore(sweeper._ACTIVE_KEY, live) is not None         # live kept


def test_reconcile_keeps_entry_that_still_exists_in_zep(
    conn, monkeypatch, captured_messages
):
    """F1 REGRESSION: a ledger entry absent from the listing (simulating an
    under-reported total_count / a dropped page) but whose graph.get RETURNS the
    graph is a LIVE entry the listing missed - it must NOT be closed; it is
    surfaced as reconcile_unconfirmed and alerted. Revert-proof: the old
    listing-completeness close would tombstone this live entry (ledger_drift=1)."""
    live_seen = scratch_id(330)                   # in the listing
    missed = scratch_id(331)                       # in the ledger, but still EXISTS
    api = install_client(
        monkeypatch, [[FakeGraph(live_seen, iso_ago(1))]],
        total_count=1, get_exists_ids=[missed],
    )
    now = int(time.time())
    conn.zadd(sweeper._ACTIVE_KEY, {live_seen: now, missed: now})

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    # (ledger_drift first: on a revert to the completeness gate this is 1, so the
    # assertion trips before any new-field access.)
    assert stats.ledger_drift == 0                                   # nothing closed
    assert stats.reconcile_unconfirmed == 1                          # gap surfaced
    assert conn.zscore(sweeper._ACTIVE_KEY, missed) is not None      # LIVE entry kept
    assert conn.zscore(sweeper._ACTIVE_KEY, live_seen) is not None
    assert any("reconcile_unconfirmed" in msg for msg, _ in captured_messages)


def test_reconcile_uncertain_get_error_fails_closed(
    conn, monkeypatch, captured_messages
):
    """graph.get raising a NON-404 error is UNCERTAIN: the entry is NOT closed
    (fail-closed) and is counted unconfirmed."""
    live = scratch_id(340)
    uncertain = scratch_id(341)                    # in ledger, get raises a 500
    api = install_client(
        monkeypatch, [[FakeGraph(live, iso_ago(1))]],
        total_count=1, get_error_ids=[uncertain],
    )
    now = int(time.time())
    conn.zadd(sweeper._ACTIVE_KEY, {live: now, uncertain: now})

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert stats.ledger_drift == 0                                   # NOT closed
    assert stats.reconcile_unconfirmed == 1
    assert conn.zscore(sweeper._ACTIVE_KEY, uncertain) is not None   # kept fail-closed


def test_sweep_memory_independent_of_nonscratch_population(
    conn, monkeypatch, captured_messages
):
    """F6: per-scan bookkeeping is O(distinct SCRATCH graphs), not O(all listed
    ids). A listing dominated by 500 non-scratch (merchant_*) ids retains only
    the distinct-scratch set (size == the scratch count); no per-all-ids set is
    kept. Revert-proof: reintroducing ``distinct_ids_seen`` (the reverted design)
    re-adds an id set that grows with the non-scratch population."""
    import inspect

    scratch = [scratch_id(300), scratch_id(301)]                 # 2 distinct scratch
    non_scratch = [f"merchant_{i:016x}" for i in range(500)]     # 500 store graphs
    graphs = [FakeGraph(g, iso_ago(1)) for g in non_scratch + scratch]
    install_client(monkeypatch, [graphs], total_count=len(graphs))

    stats = sweeper.sweep_orphan_graphs(
        dry_run=True, ttl_hours=24, page_size=1000, max_deletes=200
    )

    # The sweep completes; the retained scratch set (== stats.matched) is keyed
    # to the DISTINCT SCRATCH count, NOT the 500 non-scratch ids.
    assert stats.scanned == 502
    assert stats.matched == len(scratch)
    # Revert-proof teeth: the O(all-distinct-ids) structure is gone from the scan.
    src = inspect.getsource(sweeper.sweep_orphan_graphs)
    assert "distinct_ids_seen" not in src
