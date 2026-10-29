import copy
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from flask import Flask

from cart_recovery.adaptive.journal import Journal, Conflict
from cart_recovery.adaptive.planning import (
    Planner,
    POLICY,
    digest,
    questions,
    reduce_answers,
)
from cart_recovery.adaptive.http import bp, PATH, signature
from cart_recovery.intelligence.typesafe import Client
from .fixtures import fixture, answer

DSN = os.getenv("AF06_TEST_DATABASE_URL")


@pytest.fixture
def setup():
    if not DSN:
        pytest.skip("AF06_TEST_DATABASE_URL required for PostgreSQL integration")
    assert "af06_" in DSN and ("127.0.0.1" in DSN or "localhost" in DSN), (
        "local AF06 test database only"
    )
    clients = []
    with psycopg.connect(DSN) as db:
        exists = db.execute("SELECT to_regclass('af6_deployments')").fetchone()[0]
        if not exists:
            db.execute(
                (
                    Path(__file__).resolve().parents[1] / "migrations/001_journal.sql"
                ).read_text()
            )

    with psycopg.connect(DSN) as db:
        if not db.execute("SELECT to_regclass('af11_erasure_fences')").fetchone()[0]:
            db.execute(
                (
                    Path(__file__).resolve().parents[1] / "migrations/002_erasure.sql"
                ).read_text()
            )

    def build(handler=None, policy=None, env=None):
        calls = []
        policy = copy.deepcopy(policy or POLICY)
        env = env or str(uuid4())

        def transport(req):
            value = json.loads(req.content)
            calls.append(value)
            return (
                handler(value) if handler else httpx.Response(200, json=answer(value))
            )

        client = Client(
            key="fixture-key",
            approved_models=["typesafe/jev-1.13"],
            transport=httpx.MockTransport(transport),
        )
        clients.append(client)
        journal = Journal(DSN, client, policy, env)
        with psycopg.connect(DSN) as db:
            db.execute(
                "INSERT INTO af6_budget_policies VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING",
                (env, json.dumps(policy["limits_micros"])),
            )
            db.execute(
                "INSERT INTO af6_threshold_registry VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING",
                (digest(policy), json.dumps(policy)),
            )
            db.execute(
                "INSERT INTO af6_deployments(environment,binding,enabled,threshold_sha256) VALUES(%s,%s,true,%s) ON CONFLICT DO NOTHING",
                (env, journal.deployment, digest(policy)),
            )
        return Planner(journal, policy), calls

    yield build
    for c in clients:
        c.close()


def evaluate(p, b, key=None):
    return p.evaluate(b, b["context"]["merchant_id"], key or str(uuid4()))


def test_frozen_replay_across_workers_and_auth_refresh(setup):
    p, calls = setup()
    b = fixture()
    r = evaluate(p, b, "first")
    assert r["plan"]["decision"]["action"] == "message"
    p2, _ = setup(env=p.journal.environment)
    assert evaluate(p2, b, "first") == r
    b["context"]["control_revision"] += 1
    b["snapshot_sha256"] = "c" * 64
    r2 = evaluate(p2, b, "new-auth")
    assert (
        r2["diagnostics"]["cache_hit"] is True
        and r2["plan"]["binding"]["control_revision"] == 2
    )
    assert len(calls) == 1 and r2["plan"]["binding"] != r["plan"]["binding"]


def test_threshold_registry_change_reuses_raw_not_authority(setup):
    p, calls = setup()
    b = fixture()
    assert evaluate(p, b, "old")["plan"]["decision"]["action"] == "message"
    policy = copy.deepcopy(POLICY)
    policy["version"] = "stricter-test"
    policy["tf04_confidence"] = 0.99
    p2, calls2 = setup(policy=policy, env=p.journal.environment)
    with psycopg.connect(DSN) as db:
        db.execute(
            "UPDATE af6_deployments SET threshold_sha256=%s WHERE environment=%s",
            (digest(policy), p.journal.environment),
        )
    with pytest.raises(Conflict):
        evaluate(p, b, "stale-worker")
    with pytest.raises(Conflict):
        evaluate(p2, b, "old")
    r = evaluate(p2, b, "new")
    assert r["plan"]["decision"]["action"] == "review" and r["diagnostics"]["cache_hit"]
    assert len(calls) == 1 and not calls2


def test_low_confidence_never_resampled(setup):
    def handle(v):
        return httpx.Response(
            200,
            json=answer(
                v, lambda x: x["answers"]["TF04:shipping"].update(confidence=0.1)
            ),
        )

    p, calls = setup(handle)
    b = fixture()
    for _ in range(3):
        assert evaluate(p, b)["plan"]["decision"]["action"] == "review"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["answers"]["TF03:shipping"]["legend"].update({"3": "invented"}),
        lambda r: r["answers"]["TF01:delta"]["probabilities"].update(
            {"useful_change": 0.001}
        ),
        lambda r: r["answers"].pop("TF04:shipping"),
        lambda r: r.update(model="other-approved"),
    ],
)
def test_malformed_whole_batch_fails_closed(setup, mutation):
    p, calls = setup(lambda v: httpx.Response(200, json=answer(v, mutation)))
    b = fixture()
    for _ in range(2):
        assert evaluate(p, b)["plan"]["decision"]["action"] == "review"
    assert len(calls) == 1


def test_duplicate_json_keys_rejected(setup):
    def handle(v):
        return httpx.Response(
            200,
            content=json.dumps(answer(v)).replace(
                '"confidence": 0.95', '"confidence": 0.2, "confidence": 0.95', 1
            ),
        )

    p, _ = setup(handle)
    assert evaluate(p, fixture())["plan"]["decision"]["action"] == "review"


def test_source_tenant_hash_history_and_scope_guards(setup):
    p, calls = setup()
    b = fixture()
    for mutate in [
        lambda x: x["sources"]["buyer"]["value"].update(merchant_id=str(uuid4())),
        lambda x: x.update(ordinal=3),
        lambda x: x["purposes"][0].update(fact_ids=["unknown"]),
        lambda x: x["spans"][0].update(attribution="shopper_report"),
        lambda x: x["context"].update(fact_set_sha256="f" * 64),
    ]:
        bad = copy.deepcopy(b)
        mutate(bad)
        with pytest.raises(ValueError):
            evaluate(p, bad)
    assert not calls


def test_incomplete_inputs_no_paid_work(setup):
    p, calls = setup()
    b = fixture()
    b["context"]["coverage"]["history_complete"] = False
    assert evaluate(p, b)["plan"]["decision"]["action"] == "review" and not calls


def test_concurrent_replica_lease(setup):
    entered = threading.Event()
    release = threading.Event()

    def handle(v):
        entered.set()
        assert release.wait(2)
        return httpx.Response(200, json=answer(v))

    p, calls = setup(handle)
    p2, calls2 = setup(env=p.journal.environment)
    b = fixture()
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(evaluate, p, b)
        assert entered.wait(1)
        r = evaluate(p2, b)
        assert r["plan"]["decision"]["action"] == "wait"
        release.set()
        assert future.result()["plan"]["decision"]["action"] == "message"
    assert len(calls) == 1 and not calls2


def test_attempt_cap_unknown_usage_and_circuit(setup):
    p, calls = setup(lambda v: httpx.Response(503))
    b = fixture()
    assert evaluate(p, b)["plan"]["decision"]["action"] == "wait" and len(calls) == 2
    assert evaluate(p, b)["plan"]["decision"]["action"] == "wait" and len(calls) == 2
    with psycopg.connect(DSN) as db:
        db.execute(
            "UPDATE af6_requests SET retry_at=clock_timestamp()-interval '1 second' WHERE environment=%s",
            (p.journal.environment,),
        )
    evaluate(p, b)
    assert len(calls) == 3
    with psycopg.connect(DSN) as db:
        assert (
            db.execute(
                "SELECT sum(charged) FROM af6_attempts WHERE environment=%s",
                (p.journal.environment,),
            ).fetchone()[0]
            == 30000
        )
        assert db.execute(
            "SELECT open_until>clock_timestamp() FROM af6_circuits WHERE environment=%s",
            (p.journal.environment,),
        ).fetchone()[0]
    evaluate(p, b)
    assert len(calls) == 3


def test_shared_message_and_environment_budgets(setup):
    policy = copy.deepcopy(POLICY)
    policy["limits_micros"]["message"] = 10000
    policy["limits_micros"]["environment"] = 10000
    p, calls = setup(
        lambda v: httpx.Response(
            200, json={k: x for k, x in answer(v).items() if k != "usage"}
        ),
        policy,
    )
    b = fixture()
    evaluate(p, b)
    changed = copy.deepcopy(b)
    changed["purposes"][0]["description"] = "A changed semantic question."
    assert evaluate(p, changed)["plan"]["decision"]["action"] == "review"
    assert (
        evaluate(p, fixture())["plan"]["decision"]["action"] == "review"
        and len(calls) == 1
    )


def test_cost_overrun_stops_selection(setup):
    p, calls = setup(
        lambda v: httpx.Response(
            200, json=answer(v, lambda r: r.update(usage={"cost": 0.02}))
        )
    )
    b = fixture()
    assert evaluate(p, b)["plan"]["decision"]["action"] == "review"
    assert (
        evaluate(p, fixture())["plan"]["decision"]["action"] == "review"
        and len(calls) == 2
    )  # unrelated merchant makes its own attempt
    b["purposes"][0]["description"] = "Changed"
    assert evaluate(p, b)["plan"]["decision"]["action"] == "review"


def test_deployment_change_during_inference_rejects_result(setup):
    p, calls = setup()
    b = fixture()

    def handle(req):
        with psycopg.connect(DSN) as db:
            db.execute(
                "UPDATE af6_deployments SET enabled=false WHERE environment=%s",
                (p.journal.environment,),
            )
        return httpx.Response(200, json=answer(json.loads(req.content)))

    p.journal.client.http.close()
    p.journal.client.http = httpx.Client(transport=httpx.MockTransport(handle))
    with pytest.raises(Conflict):
        evaluate(p, b)
    with psycopg.connect(DSN) as db:
        assert (
            db.execute(
                "SELECT selected FROM af6_requests WHERE environment=%s",
                (p.journal.environment,),
            ).fetchone()[0]
            is None
        )


def test_reducer_novelty_and_margin():
    b = fixture()
    r = answer({"questions": questions(b)})["answers"]
    assert (
        reduce_answers(b, r, POLICY)[0]["action"] == "message"
    )  # no-useful-change is not a veto
    r["TF04:shipping"]["choice"] = "repeats"
    assert reduce_answers(b, r, POLICY)[0]["action"] == "wait"
    b["purposes"].append({**b["purposes"][0], "id": "second"})
    r = answer({"questions": questions(b)})["answers"]
    assert reduce_answers(b, r, POLICY)[0]["action"] == "review"
    r["TF03:second"].update(confidence=0.2)
    assert reduce_answers(b, r, POLICY)[0]["action"] == "message"


def test_endpoint_auth_replay_and_default_off(setup, monkeypatch):
    p, calls = setup()
    app = Flask(__name__)
    app.register_blueprint(bp)
    app.extensions["adaptive_planner"] = p
    client = app.test_client()
    b = fixture()
    raw = json.dumps(b).encode()
    mid = b["context"]["merchant_id"]
    ts = str(int(time.time()))
    headers = {
        "X-API-Key": "api",
        "X-Merchant-Id": mid,
        "Idempotency-Key": "key",
        "X-Wakaru-Timestamp": ts,
        "X-Wakaru-Signature": signature("secret", ts, mid, "key", raw),
    }
    monkeypatch.delenv("ADAPTIVE_FOLLOWUP_PLANNING_ENABLED", raising=False)
    assert client.post(PATH, data=raw, headers=headers).status_code == 503
    monkeypatch.setenv("ADAPTIVE_FOLLOWUP_PLANNING_ENABLED", "true")
    monkeypatch.setenv("WAKARU_API_KEY", "api")
    monkeypatch.setenv("WAKARU_INTERNAL_SECRET", "secret")
    assert (
        client.post(
            PATH, data=raw, headers={**headers, "X-Merchant-Id": str(uuid4())}
        ).status_code
        == 401
    )
    assert client.post(PATH, data=raw, headers=headers).status_code == 200
    assert (
        client.post(PATH, data=raw, headers=headers).status_code == 200
        and len(calls) == 1
    )
    b["message_revision"] += 1
    raw = json.dumps(b).encode()
    headers["X-Wakaru-Signature"] = signature("secret", ts, mid, "key", raw)
    assert client.post(PATH, data=raw, headers=headers).status_code == 409


def test_arm_treatment_starts_at_second_message(setup):
    p, calls = setup()
    b = fixture()
    b["context"]["experiment_arm"] = "first_email_only"
    assert (
        evaluate(p, b)["plan"]["decision"] == {"action": "stop", "reason": "stopped"}
        and not calls
    )
    b["context"]["experiment_arm"] = "rules_selected_followups"
    assert evaluate(p, b)["plan"]["decision"]["action"] == "message" and not calls
    b["ordinal"] = 1
    b["context"]["conversation"]["confirmed_messages"] = []
    assert evaluate(p, b)["plan"]["decision"]["action"] == "message" and len(calls) == 1


def test_timeout_never_uses_late_result(setup):
    def slow(v):
        time.sleep(0.15)
        return httpx.Response(200, json=answer(v))

    p, calls = setup(slow)
    p.journal.client.deadline = 0.05
    b = fixture()
    r = evaluate(p, b)
    assert r["plan"]["decision"]["action"] == "wait"
    time.sleep(0.2)
    with psycopg.connect(DSN) as db:
        assert (
            db.execute(
                "SELECT selected FROM af6_requests WHERE environment=%s",
                (p.journal.environment,),
            ).fetchone()[0]
            is None
        )
        assert (
            db.execute(
                "SELECT charged FROM af6_attempts WHERE environment=%s",
                (p.journal.environment,),
            ).fetchone()[0]
            == POLICY["reservation_micros"]
        )
    assert len(calls) == 1


def test_crash_before_selection_retains_cost_and_fences_stale_worker(setup):
    p, calls = setup()
    b = fixture()
    normal = p.journal.client.evaluate

    def crash(request, before, finish):
        before(str(uuid4()))
        raise RuntimeError("simulated crash")

    p.journal.client.evaluate = crash
    with pytest.raises(RuntimeError):
        evaluate(p, b)
    p.journal.client.evaluate = normal
    assert evaluate(p, b)["plan"]["decision"]["action"] == "wait"
    with psycopg.connect(DSN) as db:
        db.execute(
            "UPDATE af6_requests SET lease_until=clock_timestamp()-interval '1 second' WHERE environment=%s",
            (p.journal.environment,),
        )
    assert evaluate(p, b)["plan"]["decision"]["action"] == "message"
    with psycopg.connect(DSN) as db:
        amounts = db.execute(
            "SELECT charged,finished_at IS NULL FROM af6_attempts WHERE environment=%s ORDER BY started_at",
            (p.journal.environment,),
        ).fetchall()
        assert amounts == [(10000, True), (1000, False)]


def test_lost_lease_rejects_paid_result(setup):
    p, calls = setup()
    b = fixture()

    def steal(req):
        with psycopg.connect(DSN) as db:
            db.execute(
                "UPDATE af6_requests SET lease_token=%s WHERE environment=%s",
                (str(uuid4()), p.journal.environment),
            )
        return httpx.Response(200, json=answer(json.loads(req.content)))

    p.journal.client.http.close()
    p.journal.client.http = httpx.Client(transport=httpx.MockTransport(steal))
    with pytest.raises(Conflict):
        evaluate(p, b)
    with psycopg.connect(DSN) as db:
        assert (
            db.execute(
                "SELECT selected FROM af6_requests WHERE environment=%s",
                (p.journal.environment,),
            ).fetchone()[0]
            is None
        )


def test_competing_requests_share_atomic_environment_cap(setup):
    policy = copy.deepcopy(POLICY)
    policy["limits_micros"]["environment"] = 10000
    entered = threading.Event()
    release = threading.Event()

    def handle(v):
        entered.set()
        release.wait(2)
        return httpx.Response(200, json=answer(v))

    p, calls = setup(handle, policy)
    p2, calls2 = setup(policy=policy, env=p.journal.environment)
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(evaluate, p, fixture())
        assert entered.wait(1)
        assert evaluate(p2, fixture())["plan"]["decision"]["action"] == "review"
        release.set()
        assert future.result()["plan"]["decision"]["action"] == "message"
    assert len(calls) == 1 and not calls2


def test_judgment_bundle_is_complete_and_threshold_bound(setup):
    from cart_recovery.adaptive.planning import contracts

    p, _ = setup()
    r = evaluate(p, fixture())
    contracts.verify_bundle(r["judgment_request"], r["semantic_bundle"])
    assert r["plan"]["judgment_ids"] == [
        j["judgment_id"] for j in r["semantic_bundle"]["judgments"]
    ]
    assert all(
        j["binding"] == r["plan"]["binding"] for j in r["semantic_bundle"]["judgments"]
    )
    assert "TF08" not in {q["task_id"] for q in r["judgment_request"]["questions"]}


def test_expired_derived_replay_is_rejected(setup):
    from datetime import datetime, timezone, timedelta

    p, _ = setup()
    now = datetime.now(timezone.utc)
    b = fixture(now)
    p.evaluate(b, b["context"]["merchant_id"], "frozen", now=now)
    with pytest.raises(Conflict):
        p.evaluate(
            b, b["context"]["merchant_id"], "frozen", now=now + timedelta(seconds=31)
        )


def test_real_http_to_postgres_to_mock_provider(setup, monkeypatch):
    from werkzeug.serving import make_server

    p, calls = setup()
    app = Flask(__name__)
    app.register_blueprint(bp)
    app.extensions["adaptive_planner"] = p
    monkeypatch.setenv("ADAPTIVE_FOLLOWUP_PLANNING_ENABLED", "true")
    monkeypatch.setenv("WAKARU_API_KEY", "fixture-api")
    monkeypatch.setenv("WAKARU_INTERNAL_SECRET", "fixture-secret")
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        b = fixture()
        raw = json.dumps(b).encode()
        mid = b["context"]["merchant_id"]
        ts = str(int(time.time()))
        key = "network-fixture"
        headers = {
            "X-API-Key": "fixture-api",
            "X-Merchant-Id": mid,
            "Idempotency-Key": key,
            "X-Wakaru-Timestamp": ts,
            "X-Wakaru-Signature": signature("fixture-secret", ts, mid, key, raw),
        }
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.post(
                f"http://127.0.0.1:{server.server_port}" + PATH,
                content=raw,
                headers=headers,
            )
            assert (
                response.status_code == 200
                and response.json()["plan"]["decision"]["action"] == "message"
            )
            replay = client.post(
                f"http://127.0.0.1:{server.server_port}" + PATH,
                content=raw,
                headers=headers,
            )
            assert replay.json() == response.json() and len(calls) == 1
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()


@pytest.mark.parametrize(
    "scope", ["batch", "message", "journey", "merchant", "environment"]
)
def test_each_budget_scope_enforced_independently(setup, scope):
    policy = copy.deepcopy(POLICY)
    policy["limits_micros"][scope] = policy["reservation_micros"]

    def unknown(v):
        return (
            httpx.Response(503)
            if scope == "batch"
            else httpx.Response(
                200, json={k: x for k, x in answer(v).items() if k != "usage"}
            )
        )

    p, calls = setup(unknown, policy)
    b = fixture()
    first = evaluate(p, b)
    if scope == "batch":
        assert first["plan"]["decision"]["action"] == "review" and len(calls) == 1
        return
    assert first["plan"]["decision"]["action"] == "message"
    b["purposes"][0]["description"] = "Changed question"
    if scope in {"journey", "merchant"}:
        b["message_id"] = str(uuid4())
    if scope == "merchant":
        b["context"]["journey_id"] = str(uuid4())
        b["context"]["conversation"]["journey_id"] = b["context"]["journey_id"]
    if scope == "environment":
        b = fixture()
    assert evaluate(p, b)["plan"]["decision"]["action"] == "review" and len(calls) == 1


def test_half_open_circuit_allows_only_one_probe(setup):
    entered = threading.Event()
    release = threading.Event()

    def handle(v):
        entered.set()
        release.wait(2)
        return httpx.Response(200, json=answer(v))

    p, calls = setup(handle)
    p2, calls2 = setup(env=p.journal.environment)
    b = fixture()
    other = copy.deepcopy(b)
    other["message_id"] = str(uuid4())
    with psycopg.connect(DSN) as db:
        db.execute(
            "INSERT INTO af6_circuits(environment,merchant,failures,open_until) VALUES(%s,%s,2,clock_timestamp()-interval '1 second')",
            (p.journal.environment, b["context"]["merchant_id"]),
        )
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(evaluate, p, b)
        assert entered.wait(1)
        assert evaluate(p2, other)["plan"]["decision"]["action"] == "review"
        release.set()
        assert future.result()["plan"]["decision"]["action"] == "message"
    assert len(calls) == 1 and not calls2


def test_separate_workflow_deployment_cannot_duplicate_message_budget(setup):
    policy = copy.deepcopy(POLICY)
    policy["limits_micros"]["message"] = 10000
    p, calls = setup(
        lambda v: httpx.Response(
            200, json={k: x for k, x in answer(v).items() if k != "usage"}
        ),
        policy,
    )
    b = fixture()
    evaluate(p, b)
    draft = Journal(
        DSN,
        p.journal.client,
        policy,
        p.journal.environment,
        workflow="draft",
        rubric={"version": "fixture-draft"},
    )
    with psycopg.connect(DSN) as db:
        db.execute(
            "INSERT INTO af6_deployments(environment,workflow,binding,enabled,threshold_sha256) VALUES(%s,%s,%s,true,%s)",
            (draft.environment, "draft", draft.deployment, digest(policy)),
        )
    identity = {
        "phase": "draft",
        "merchant_id": b["context"]["merchant_id"],
        "journey_id": b["context"]["journey_id"],
        "message_id": b["message_id"],
        "message_revision": b["message_revision"],
    }
    r = draft.judge(
        identity,
        {"state": {"fixture": "budget authority only"}, "questions": questions(b)},
    )
    assert (
        r["status"] == "unavailable"
        and r["error_class"] == "semantic_budget_exhausted"
        and len(calls) == 1
    )
    policy["limits_micros"]["message"] = 20000
    with psycopg.connect(DSN) as db:
        db.execute(
            "INSERT INTO af6_threshold_registry VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING",
            (digest(policy), json.dumps(policy)),
        )
        db.execute(
            "UPDATE af6_deployments SET threshold_sha256=%s WHERE environment=%s AND workflow='draft'",
            (digest(policy), draft.environment),
        )
    with pytest.raises(Conflict):
        draft.revision()


def test_explicit_report_cannot_pass_without_literal_attribution(setup):
    def report(v):
        r = answer(v)
        a = r["answers"]["TF02:shipping"]
        a["choice"] = "explicit_report"
        a["probabilities"] = {
            k: int(k == "explicit_report") for k in a["probabilities"]
        }
        return httpx.Response(200, json=r)

    p, _ = setup(report)
    r = evaluate(p, fixture())
    assert r["plan"]["decision"]["action"] == "review"
    assert (
        next(j for j in r["semantic_bundle"]["judgments"] if j["task_id"] == "TF02")[
            "disposition"
        ]
        == "requires_review"
    )
