"""Malformed paid responses must settle their attempts without selecting answers."""

import copy
import json

import httpx
import pytest

from .test_typesafe import CONTRACTS
from .typesafe import RESERVATION, Client, Store, validate


@pytest.fixture
def paid_judgment(tmp_path):
    clients = []

    def run(fixture, cost):
        response = fixture["response"]
        response["usage"] = {"cost": cost}
        client = Client(
            key="synthetic",
            approved_models=["typesafe/jev-1.13-20260917"],
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=json.dumps(response))
            ),
        )
        clients.append(client)
        store = Store(tmp_path / "journal.db", client)
        identity = dict(
            merchant_id="m", workflow="draft_claims", revision="1",
            snapshot_hash="h", rubric_hash="r", policy_version="v",
            candidate_hash="c", as_of="2026-10-25", episode_id="e",
        )
        result = store.judge(identity, fixture["request"])["result"]
        assert result["status"] == "unresolved"
        assert len(result["attempts"]) == 1
        attempt = result["attempts"][0]
        assert attempt["error_class"] == "schema_failure"
        assert attempt["cost"] == cost
        assert attempt["finished_at"] >= attempt["started_at"]
        with store.connection() as db:
            for table in ("attempts", "results", "judgments", "raw_outputs"):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
            for table in ("selected", "leases"):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            saved = json.loads(db.execute("SELECT result FROM results").fetchone()[0])
            assert saved == attempt
            spent = [row[0] for row in db.execute("SELECT spent FROM budgets")]
            assert spent == pytest.approx([max(RESERVATION, cost)] * 3)
        return result

    yield run
    for client in clients:
        client.close()


@pytest.mark.parametrize("field,fixture_name", [
    ("confidence", "choice_valid"), ("probabilities", "choice_valid"),
    ("noul", "noul_valid"), ("score", "score_valid"),
])
@pytest.mark.parametrize("sign", [-1, 1])
def test_oversized_answer_is_rejected_and_settled(paid_judgment, field, fixture_name, sign):
    fixtures = json.loads((CONTRACTS / "conformance.json").read_text())
    fixture = copy.deepcopy(next(f for f in fixtures if f["name"] == fixture_name))
    answer = next(iter(fixture["response"]["answers"].values()))
    if field == "probabilities":
        answer[field][next(iter(answer[field]))] = sign * 10**400
    else:
        answer[field] = sign * 10**400
    result = paid_judgment(fixture, 0.0001)
    assert result["error_class"] == "schema_failure"
    with pytest.raises(ValueError, match="invalid_"):
        validate(fixture["request"], fixture["response"])


@pytest.mark.parametrize("field", ["answers", "model"])
@pytest.mark.parametrize("cost", [0.0001, 0.02])
def test_reported_cost_survives_invalid_answer_or_model(paid_judgment, field, cost):
    fixture = json.loads((CONTRACTS / "conformance.json").read_text())[0]
    fixture["response"][field] = None
    result = paid_judgment(fixture, cost)
    expected = "cost_reservation_exceeded" if cost > RESERVATION else "schema_failure"
    assert result["error_class"] == expected
