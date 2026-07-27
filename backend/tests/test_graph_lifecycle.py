"""Issue #72 - graph-lifecycle ledger (app/services/graph_lifecycle.py).

Redis key schema per docs/specs/issue-72 SCHEMA sections 2.1-2.3:
zep:graph:<id> hash, zep:scratch:active zset, zep:merchant:<id>:graphs set.
Backed by fakeredis (the suite convention - see test_idempotency.py). The
ledger's contract is diagnostic-only: Redis unavailability degrades every
function to a warn-and-no-op, never an exception, because a ledger outage
must never fail a paid analysis or a sweep.
"""
import logging

import fakeredis
import pytest
from redis.exceptions import RedisError

import app.services.graph_lifecycle as gl

GRAPH_A = "mirofish_0123456789abcdef"
GRAPH_B = "mirofish_fedcba9876543210"
MERCHANT = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def conn(monkeypatch):
    conn = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(gl, "get_redis_connection", lambda: conn)
    return conn


# --- record_created ------------------------------------------------------------

def test_record_created_writes_hash_zset_and_merchant_set(conn):
    gl.record_created(GRAPH_A, MERCHANT)

    record = {k.decode(): v.decode() for k, v in conn.hgetall(f"zep:graph:{GRAPH_A}").items()}
    assert record["graph_kind"] == "scratch"
    assert record["merchant_id"] == MERCHANT
    assert record["created_at"].isdigit()
    assert conn.ttl(f"zep:graph:{GRAPH_A}") == -1  # no TTL while alive

    score = conn.zscore("zep:scratch:active", GRAPH_A)
    assert score == int(record["created_at"])
    assert conn.smembers(f"zep:merchant:{MERCHANT}:graphs") == {GRAPH_A.encode()}


@pytest.mark.parametrize("bad_id", [
    "merchant_0123456789abcdef",   # #61 store graph
    "mirofish_SHOUTING1234567",    # wrong case
    "mirofish_0123456789abcde",    # 15 hex
    "mirofish_0123456789abcdef0",  # 17 hex
    "mirofish_0123456789abcdef\n", # trailing newline
    "",
])
def test_record_created_structurally_rejects_non_scratch_ids(conn, bad_id):
    # SCHEMA 2.1: a non-scratch id reaching record_created is a programming
    # error, not a data state - fail loud, write nothing.
    with pytest.raises(ValueError, match="not a scratch graph id"):
        gl.record_created(bad_id, MERCHANT)
    assert conn.keys("zep:*") == []


# --- record_deleted ------------------------------------------------------------

def test_record_deleted_stamps_and_removes_from_registries(conn):
    gl.record_created(GRAPH_A, MERCHANT)
    gl.record_deleted(GRAPH_A, source="inline")

    record = {k.decode(): v.decode() for k, v in conn.hgetall(f"zep:graph:{GRAPH_A}").items()}
    assert record["delete_source"] == "inline"
    assert record["deleted_at"].isdigit()
    assert record["merchant_id"] == MERCHANT  # attribution history retained

    # 30-day diagnostic TTL on the deleted record (SCHEMA 2.1).
    assert 0 < conn.ttl(f"zep:graph:{GRAPH_A}") <= gl.DELETED_RECORD_TTL_SECONDS

    assert conn.zscore("zep:scratch:active", GRAPH_A) is None
    assert conn.smembers(f"zep:merchant:{MERCHANT}:graphs") == set()


def test_record_deleted_without_created_writes_orphan_record(conn):
    # A sweeper-deleted pre-ledger orphan gets a hash written at deletion time
    # with merchant_id ABSENT - explicitly distinguishable from the sentinel.
    gl.record_deleted(GRAPH_B, source="sweep")

    record = {k.decode(): v.decode() for k, v in conn.hgetall(f"zep:graph:{GRAPH_B}").items()}
    assert record["delete_source"] == "sweep"
    assert "merchant_id" not in record
    assert 0 < conn.ttl(f"zep:graph:{GRAPH_B}") <= gl.DELETED_RECORD_TTL_SECONDS


def test_record_deleted_generation_bound_skips_a_reincarnated_id(conn):
    # F1 (revision 8): the sweep confirms an id gone from a SNAPSHOT, then closes
    # it later. If a same-id record_created re-incarnates it in between (a new
    # created_at / cleared tombstone), the generation-bound close must NOT
    # tombstone the live new generation.
    gl.record_created(GRAPH_A, MERCHANT)
    snapshot_created_at = gl.created_at_for(GRAPH_A)   # the generation we "confirmed gone"

    # Re-incarnation: same id, a DIFFERENT created_at (a later occurrence).
    conn.hset(f"zep:graph:{GRAPH_A}", "created_at", str(snapshot_created_at + 500))
    conn.zadd("zep:scratch:active", {GRAPH_A: snapshot_created_at + 500})

    # Close bound to the OLD generation must skip: the live new generation stays
    # active, un-tombstoned, still enumerable for its merchant.
    gl.record_deleted(GRAPH_A, source="sweep", expected_created_at=snapshot_created_at)
    assert conn.hget(f"zep:graph:{GRAPH_A}", "deleted_at") is None      # not tombstoned
    assert conn.zscore("zep:scratch:active", GRAPH_A) is not None       # still active
    assert GRAPH_A.encode() in conn.smembers(f"zep:merchant:{MERCHANT}:graphs")

    # A close bound to the CURRENT generation DOES proceed.
    gl.record_deleted(GRAPH_A, source="sweep", expected_created_at=snapshot_created_at + 500)
    assert conn.hget(f"zep:graph:{GRAPH_A}", "deleted_at") is not None  # now closed
    assert conn.zscore("zep:scratch:active", GRAPH_A) is None


# --- readers -------------------------------------------------------------------

def test_created_at_for_roundtrip_and_missing(conn):
    gl.record_created(GRAPH_A, MERCHANT)
    created_at = gl.created_at_for(GRAPH_A)
    assert isinstance(created_at, int)
    assert created_at > 0
    assert gl.created_at_for(GRAPH_B) is None


def test_created_at_for_malformed_value_returns_none(conn):
    conn.hset(f"zep:graph:{GRAPH_A}", "created_at", "not-a-number")
    assert gl.created_at_for(GRAPH_A) is None


def test_graphs_for_merchant_enumerates_live_graphs_only(conn):
    gl.record_created(GRAPH_A, MERCHANT)
    gl.record_created(GRAPH_B, MERCHANT)
    assert gl.graphs_for_merchant(MERCHANT) == sorted([GRAPH_A, GRAPH_B])

    gl.record_deleted(GRAPH_A, source="inline")
    assert gl.graphs_for_merchant(MERCHANT) == [GRAPH_B]
    assert gl.graphs_for_merchant("22222222-2222-4222-8222-222222222222") == []


# --- Redis unavailable: warn + no-op, never raise -------------------------------

def test_all_functions_noop_when_redis_unconfigured(monkeypatch, caplog):
    # get_redis_connection() returns None when REDIS_URL is unset - every
    # ledger function must degrade to a warning, never fail the caller.
    monkeypatch.setattr(gl, "get_redis_connection", lambda: None)
    monkeypatch.setattr(logging.getLogger("mirofish"), "propagate", True)

    with caplog.at_level(logging.WARNING, logger="mirofish.cart_recovery"):
        gl.record_created(GRAPH_A, MERCHANT)
        gl.record_deleted(GRAPH_A, source="inline")
        assert gl.created_at_for(GRAPH_A) is None
        assert gl.graphs_for_merchant(MERCHANT) == []

    warnings = [r for r in caplog.records if "Redis unavailable" in r.getMessage()]
    assert len(warnings) == 4


def test_all_functions_noop_on_redis_error(monkeypatch):
    # A connected-but-failing Redis (outage mid-command) degrades the same way.
    class BoomConn:
        def pipeline(self):
            raise RedisError("boom")

        def hget(self, *a):
            raise RedisError("boom")

        def hgetall(self, *a):
            raise RedisError("boom")

        def smembers(self, *a):
            raise RedisError("boom")

    monkeypatch.setattr(gl, "get_redis_connection", lambda: BoomConn())

    gl.record_created(GRAPH_A, MERCHANT)   # must not raise
    gl.record_deleted(GRAPH_A, source="inline")
    assert gl.created_at_for(GRAPH_A) is None
    assert gl.graphs_for_merchant(MERCHANT) == []


def test_invalid_id_rejected_even_without_redis(monkeypatch):
    # The structural guard is a programming-error check, not a Redis feature:
    # it fires before (and regardless of) connection availability.
    monkeypatch.setattr(gl, "get_redis_connection", lambda: None)
    with pytest.raises(ValueError, match="not a scratch graph id"):
        gl.record_created("merchant_0123456789abcdef", MERCHANT)
