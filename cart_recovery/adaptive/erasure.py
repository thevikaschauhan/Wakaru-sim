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
            # Serialize erasure with cost settlement. A late callback settles a
            # minimal accounting hold after raw request/answer rows are gone.
            db.execute(
                "SELECT id FROM af6_attempts WHERE environment=%s AND merchant=%s AND request_key=%s ORDER BY id FOR UPDATE",
                (env, merchant, key),
            ).fetchall()
            db.execute(
                """INSERT INTO af11_cost_holds(id,environment,merchant,lease_token,reserved,charged,allocations)
                 SELECT a.id,a.environment,a.merchant,a.lease_token,a.reserved,a.charged,
                  coalesce((SELECT jsonb_agg(jsonb_build_array(b.environment,b.scope,b.identity,b.period) ORDER BY b.scope,b.identity,b.period)
                   FROM af6_allocations b WHERE b.attempt=a.id AND b.scope<>'batch'),'[]'::jsonb)
                 FROM af6_attempts a WHERE a.environment=%s AND a.merchant=%s AND a.request_key=%s AND a.finished_at IS NULL
                 ON CONFLICT DO NOTHING""",
                (env, merchant, key),
            )
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


def settle_erased_attempt(db, environment, merchant, token, attempt, measured_micros):
    """Resolve known late usage once; never refund erased/unknown reservations."""
    row = db.execute(
        "SELECT charged,allocations,finished_at,unknown_usage FROM af11_cost_holds WHERE id=%s AND environment=%s AND merchant=%s AND lease_token=%s FOR UPDATE",
        (attempt, environment, merchant, token),
    ).fetchone()
    if not row or (row[2] is not None and (not row[3] or measured_micros is None)):
        return None
    charged = max(row[0], measured_micros) if measured_micros is not None else row[0]
    delta = charged - row[0]
    if delta:
        for env, scope, identity, period in row[1]:
            updated = db.execute(
                "UPDATE af6_budgets SET spent=spent+%s WHERE environment=%s AND scope=%s AND identity=%s AND period=%s",
                (delta, env, scope, identity, period),
            )
            if updated.rowcount != 1:
                raise ValueError("erased_cost_allocation_missing")
    db.execute(
        "UPDATE af11_cost_holds SET charged=%s,finished_at=clock_timestamp(),unknown_usage=%s WHERE id=%s",
        (charged, measured_micros is None, attempt),
    )
    return charged
