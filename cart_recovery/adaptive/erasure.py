"""Adaptive journal erasure. Aggregate charged budgets are never refunded."""

from uuid import UUID


def erase_journey(journal, merchant, journey):
    if str(UUID(merchant)) != merchant or str(UUID(journey)) != journey:
        raise ValueError("invalid_scope")
    with journal.transaction() as db:
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s||':adaptive-privacy',0))",
            (merchant,),
        )
        db.execute(
            "INSERT INTO af11_erasure_fences(merchant,journey_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (merchant, journey),
        )
        # Journey allocations recover old request bindings when a model call exists.
        keys = db.execute(
            "SELECT DISTINCT r.environment,r.request_key FROM af6_requests r WHERE r.merchant=%s AND (r.journey_id=%s OR EXISTS(SELECT 1 FROM af6_attempts a JOIN af6_allocations b ON b.attempt=a.id WHERE a.environment=r.environment AND a.merchant=r.merchant AND a.request_key=r.request_key AND b.scope='journey' AND b.identity=%s))",
            (merchant, journey, merchant + ":" + journey),
        ).fetchall()
        for env, key in keys:
            db.execute(
                "DELETE FROM af6_allocations WHERE attempt IN(SELECT id FROM af6_attempts WHERE environment=%s AND merchant=%s AND request_key=%s)",
                (env, merchant, key),
            )
            db.execute(
                "DELETE FROM af6_attempts WHERE environment=%s AND merchant=%s AND request_key=%s",
                (env, merchant, key),
            )
            db.execute(
                "DELETE FROM af6_requests WHERE environment=%s AND merchant=%s AND request_key=%s",
                (env, merchant, key),
            )
            db.execute(
                "DELETE FROM af6_budgets b WHERE environment=%s AND scope='batch' AND identity=%s AND NOT EXISTS(SELECT 1 FROM af6_allocations a WHERE a.environment=b.environment AND a.scope=b.scope AND a.identity=b.identity AND a.period=b.period)",
                (env, key),
            )
        db.execute(
            "DELETE FROM af6_evaluations WHERE merchant=%s AND journey_id=%s",
            (merchant, journey),
        )
        # Keep merchant/environment totals, including unknown reservations. Journey
        # and message budget identities remain suppression metadata until an approved
        # tombstone lifecycle applies. They contain no prompt, answer or email.
        unmapped = db.execute(
            "SELECT count(*) FROM af6_evaluations WHERE merchant=%s AND journey_id IS NULL",
            (merchant,),
        ).fetchone()[0]
    if unmapped:
        raise ValueError("legacy_evaluation_scope_unmapped")
    return {"source": "wakaru", "journey_id": journey, "erased": True}
