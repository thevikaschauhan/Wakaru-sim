from uuid import uuid4

import httpx
import psycopg
import pytest

from cart_recovery.adaptive.erasure import erase_journey, settle_erased_attempt
from cart_recovery.adaptive.journal import Conflict
from cart_recovery.adaptive.planning import POLICY, questions
from .fixtures import answer, fixture
from .test_planner import DSN, setup  # noqa: F401


@pytest.mark.parametrize("cost", [None, 0.002, 0.02])
def test_inflight_erasure_retains_late_usage_and_idempotent_binding(setup, cost):  # noqa: F811
    build = setup
    evidence = fixture()
    merchant, journey = (
        evidence["context"]["merchant_id"],
        evidence["context"]["journey_id"],
    )
    erased = []

    def transport(request):
        # This runs inside the actual Client's mock transport after its durable
        # reservation but before provider completion/settlement.
        erased.append(erase_journey(journal, merchant, journey))
        value = answer(request)
        value["usage"]["cost"] = cost
        return httpx.Response(200, json=value)

    planner, _ = build(handler=transport)
    journal = planner.journal
    identity = {
        "merchant_id": merchant,
        "journey_id": journey,
        "message_id": evidence["message_id"],
        "message_revision": 0,
        "phase": "planning",
    }
    with pytest.raises(Conflict, match="semantic_erased_or_stale"):
        journal.judge(identity, {"state": {}, "questions": questions(evidence)})
    assert erased[0]["erased"] is True
    expected = (
        max(POLICY["reservation_micros"], round(cost * 1000000))
        if cost is not None
        else POLICY["reservation_micros"]
    )
    with psycopg.connect(DSN) as db:
        for table in ("af6_requests", "af6_attempts", "af6_evaluations"):
            assert (
                db.execute(
                    f"SELECT count(*) FROM {table} WHERE merchant=%s", (merchant,)
                ).fetchone()[0]
                == 0
            )
        totals = db.execute(
            "SELECT scope,spent FROM af6_budgets WHERE environment=%s AND scope IN('merchant','environment') ORDER BY scope",
            (journal.environment,),
        ).fetchall()
        assert totals == [("environment", expected), ("merchant", expected)]
        hold = db.execute(
            "SELECT id,lease_token,charged,unknown_usage FROM af11_cost_holds WHERE merchant=%s",
            (merchant,),
        ).fetchone()
        assert hold[2:] == (expected, cost is None)
        assert db.execute(
            "SELECT count(*) FROM af6_circuits WHERE environment=%s AND merchant=%s AND open_until='infinity'",
            (journal.environment, merchant),
        ).fetchone()[0] == int(cost is not None and cost > 0.01)
        # A foreign merchant/token cannot settle the hold, and a duplicate
        # completed callback cannot add another charge.
        assert (
            settle_erased_attempt(
                db, journal.environment, str(uuid4()), str(hold[1]), hold[0], expected
            )
            is None
        )
        assert (
            settle_erased_attempt(
                db, journal.environment, merchant, str(uuid4()), hold[0], expected
            )
            is None
        )
        assert (
            settle_erased_attempt(
                db,
                journal.environment,
                merchant,
                str(hold[1]),
                hold[0],
                expected if cost is not None else None,
            )
            is None
        )
        assert (
            db.execute(
                "SELECT scope,spent FROM af6_budgets WHERE environment=%s AND scope IN('merchant','environment') ORDER BY scope",
                (journal.environment,),
            ).fetchall()
            == totals
        )
