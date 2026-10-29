from .test_planner import setup as setup
import threading
from concurrent.futures import ThreadPoolExecutor
import httpx, pytest
from .fixtures import fixture, answer
from cart_recovery.adaptive.erasure import erase_journey


def test_erasure_deletes_answers_and_blocks_replayed_evaluation(setup):
    planner, calls = setup()
    body = fixture()
    m = body["context"]["merchant_id"]
    j = body["context"]["journey_id"]
    planner.evaluate(body, m, "erase-fixture")
    with planner.journal.transaction() as db:
        spent = db.execute(
            "SELECT sum(spent) FROM af6_budgets WHERE environment=%s AND scope='merchant'",
            (planner.journal.environment,),
        ).fetchone()[0]
    assert erase_journey(planner.journal, m, j)["erased"]
    with pytest.raises(Exception):
        planner.evaluate(body, m, "erase-replay")
    with planner.journal.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM af6_requests WHERE merchant=%s", (m,)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM af6_evaluations WHERE merchant=%s", (m,)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT sum(spent) FROM af6_budgets WHERE environment=%s AND scope='merchant'",
                (planner.journal.environment,),
            ).fetchone()[0]
            == spent
        )
    assert erase_journey(planner.journal, m, j)["erased"]
    assert len(calls) == 1


def test_erasure_during_model_call_never_recreates_selected_answer(setup):
    entered = threading.Event()
    release = threading.Event()

    def response(req):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json=answer(req))

    planner, calls = setup(response)
    body = fixture()
    m = body["context"]["merchant_id"]
    j = body["context"]["journey_id"]
    with ThreadPoolExecutor() as pool:
        future = pool.submit(planner.evaluate, body, m, "inflight")
        assert entered.wait(5)
        erase_journey(planner.journal, m, j)
        release.set()
        with pytest.raises(Exception):
            future.result(timeout=5)
    with planner.journal.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM af6_requests WHERE merchant=%s", (m,)
            ).fetchone()[0]
            == 0
        )
    assert len(calls) == 1
