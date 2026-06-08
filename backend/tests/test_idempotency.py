"""Issue #12 — Redis-backed idempotency helper for the paid cart-recovery
endpoints.

The helper is a small state machine over a Redis SET NX claim:
  claim_or_get -> ("won", None)      caller is first; do the work then record()
               -> ("pending", None)  a concurrent request holds the slot
               -> ("replay", value)  a prior completed value; return it
record() overwrites the PENDING marker with the final value (24h TTL).
release() frees the slot when the work was NOT done (a safe-to-retry rejection).

Backed by fakeredis so the tests need no live Redis. Synthetic keys only.
"""
import fakeredis

from app.services.idempotency import claim_or_get, record, release


def test_first_claim_wins():
    conn = fakeredis.FakeStrictRedis()
    assert claim_or_get(conn, "jobs", "k1") == ("won", None)


def test_second_claim_while_pending_returns_pending():
    conn = fakeredis.FakeStrictRedis()
    claim_or_get(conn, "jobs", "k1")  # first wins -> PENDING
    assert claim_or_get(conn, "jobs", "k1") == ("pending", None)


def test_replay_returns_recorded_value():
    conn = fakeredis.FakeStrictRedis()
    claim_or_get(conn, "jobs", "k1")
    record(conn, "jobs", "k1", "job-abc")
    assert claim_or_get(conn, "jobs", "k1") == ("replay", "job-abc")


def test_release_frees_the_slot():
    conn = fakeredis.FakeStrictRedis()
    claim_or_get(conn, "jobs", "k1")
    release(conn, "jobs", "k1")
    # slot is free again -> a fresh claim wins
    assert claim_or_get(conn, "jobs", "k1") == ("won", None)


def test_scopes_are_isolated():
    conn = fakeredis.FakeStrictRedis()
    claim_or_get(conn, "jobs", "k1")
    record(conn, "jobs", "k1", "job-abc")
    # same key under a different scope is independent
    assert claim_or_get(conn, "analyze", "k1") == ("won", None)


def test_record_sets_24h_ttl():
    conn = fakeredis.FakeStrictRedis()
    claim_or_get(conn, "jobs", "k1")
    record(conn, "jobs", "k1", "job-abc")
    ttl = conn.ttl("idem:jobs:k1")
    assert 86000 < ttl <= 86400
