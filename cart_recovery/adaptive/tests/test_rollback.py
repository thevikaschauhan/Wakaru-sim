"""Local rollback rehearsal: real authenticated HTTP and PostgreSQL journal."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg
from flask import Flask

from .test_planner import setup as setup
from .test_draft import checker as checker, draft_input
from .fixtures import fixture, answer
from ..http import bp, PATH, signature
from ..planning import POLICY, digest


def test_rollback_fences_inflight_and_preserves_charges(setup, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def provider(value):
        entered.set()
        assert release.wait(4)
        return httpx.Response(200, json=answer(value))

    p, calls = setup(handler=provider)
    env, dsn = p.journal.environment, p.journal.dsn
    app = Flask(__name__)
    app.register_blueprint(bp)
    app.extensions["adaptive_planner"] = p
    for name, value in {
        "ADAPTIVE_FOLLOWUP_PLANNING_ENABLED": "true",
        "WAKARU_API_KEY": "fixture",
        "WAKARU_INTERNAL_SECRET": "fixture-secret",
    }.items():
        monkeypatch.setenv(name, value)

    def request(body, key):
        raw = json.dumps(body).encode()
        ts = str(int(time.time()))
        merchant = body["context"]["merchant_id"]
        with app.test_client() as client:
            return client.post(
                PATH,
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": "fixture",
                    "X-Merchant-Id": merchant,
                    "Idempotency-Key": key,
                    "X-Wakaru-Timestamp": ts,
                    "X-Wakaru-Signature": signature(
                        "fixture-secret", ts, merchant, key, raw
                    ),
                },
            )

    old = fixture()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(request, old, "inflight-before-rollback")
        assert entered.wait(2)
        with psycopg.connect(dsn) as db:
            db.execute(
                "UPDATE af6_deployments SET enabled=false,binding=%s WHERE environment=%s",
                ("unqualified-candidate", env),
            )
        assert request(fixture(), "disabled").status_code == 409
        # Restore exactly the pinned model/rubric/threshold binding. The database
        # revision still advances, so restoring a hash cannot revive old work.
        with psycopg.connect(dsn) as db:
            db.execute(
                "UPDATE af6_deployments SET enabled=true,binding=%s,threshold_sha256=%s WHERE environment=%s",
                (p.journal.deployment, digest(POLICY), env),
            )
        release.set()
        assert future.result(timeout=4).status_code == 409
    with psycopg.connect(dsn) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM af6_requests WHERE environment=%s AND selected IS NOT NULL",
                (env,),
            ).fetchone()[0]
            == 0
        )
        before = db.execute(
            "SELECT coalesce(sum(charged),0) FROM af6_attempts WHERE environment=%s",
            (env,),
        ).fetchone()[0]
        assert before > 0
        revision = db.execute(
            "SELECT revision FROM af6_deployments WHERE environment=%s", (env,)
        ).fetchone()[0]
        assert revision == 2
    # New work after rollback can proceed. Old in-flight work remains rejected;
    # the already incurred cost stays charged rather than becoming a refund.
    resumed = request(fixture(), "after-rollback")
    assert resumed.status_code == 200
    assert resumed.json["plan"]["decision"]["action"] == "message"
    with psycopg.connect(dsn) as db:
        after = db.execute(
            "SELECT sum(charged) FROM af6_attempts WHERE environment=%s", (env,)
        ).fetchone()[0]
        assert after > before
    assert len(calls) == 2


def test_rollback_keeps_mandatory_draft_checks(checker):
    p, build, calls = checker

    def unsafe(result):
        result["answers"].pop("TF05:document")

    d = build(unsafe)
    with psycopg.connect(d.journal.dsn) as db:
        db.execute(
            "UPDATE af6_deployments SET enabled=false WHERE environment=%s AND workflow='draft'",
            (d.journal.environment,),
        )
        db.execute(
            "UPDATE af6_deployments SET enabled=true,binding=%s WHERE environment=%s AND workflow='draft'",
            (d.journal.deployment, d.journal.environment),
        )
    body = draft_input(p)
    out = d.evaluate(
        body, body["evaluation"]["context"]["merchant_id"], "mandatory-after-rollback"
    )
    assert out["disposition"] != "pass"
    assert calls
