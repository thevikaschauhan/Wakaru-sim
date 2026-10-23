import json
import httpx
import psycopg
import pytest
from flask import Flask
from cart_recovery.adaptive.draft import DraftChecker, POLICY, RUBRIC
from cart_recovery.adaptive.planning import digest, strict_response
from cart_recovery.adaptive.journal import Journal
from cart_recovery.adaptive.draft_http import bp, PATH, signature
from cart_recovery.intelligence.typesafe import Client
from .fixtures import fixture
from .test_planner import setup as _planner_setup, evaluate

setup = _planner_setup


def draft_input(planner):
    e = fixture()
    p = evaluate(planner, e)["plan"]
    text = e["sources"]["shipping"]["value"]["text"]
    d = {
        "revision": 0,
        "subject": "Delivery information",
        "preheader": "A detail for your order",
        "body": text,
        "claims": [
            {
                "id": "delivery",
                "surface": "body",
                "start": 0,
                "end": len(text.encode()),
                "text": text,
                "fact_id": "shipping",
                "source_quote": text,
            }
        ],
        "commitments": [],
        "cta_refs": [],
        "planning_basis": "behavior_only",
    }
    d["content_sha256"] = digest({k: d[k] for k in ("subject", "preheader", "body")})
    return {
        "schema_version": "adaptive_draft_v1",
        "evaluation": e,
        "plan": p,
        "draft": d,
    }


def response(req):
    answers = {}
    for i, q in req["questions"].items():
        if q["type"] == "noul":
            answers[i] = {"type": "noul", "noul": 0.01}
            continue
        good = {
            "TF04": "adds_useful_information",
            "TF05": "supported",
            "TF07": "consistent",
        }[i[:4]]
        answers[i] = {
            "type": "choice",
            "choice": good,
            "confidence": 0.99,
            "probabilities": {k: (1 if k == good else 0) for k in q["criteria"]},
        }
    return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {"cost": 0.001}}


@pytest.fixture
def checker(setup):
    p, _ = setup()
    calls = []

    def build(change=None):
        def transport(r):
            v = json.loads(r.content)
            calls.append(v)
            a = response(v)
            if change:
                change(a)
            return httpx.Response(200, json=a)

        client = Client(
            key="fixture",
            approved_models=["typesafe/jev-1.13"],
            transport=httpx.MockTransport(transport),
        )
        j = Journal(
            p.journal.dsn,
            client,
            POLICY,
            p.journal.environment,
            workflow="draft",
            rubric=RUBRIC,
        )
        with psycopg.connect(j.dsn) as db:
            db.execute(
                "INSERT INTO af6_threshold_registry VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING",
                (digest(POLICY), json.dumps(POLICY)),
            )
            db.execute(
                "INSERT INTO af6_deployments(environment,workflow,binding,enabled,threshold_sha256) VALUES(%s,'draft',%s,true,%s) ON CONFLICT DO NOTHING",
                (j.environment, j.deployment, digest(POLICY)),
            )
        return DraftChecker(j)

    yield p, build, calls


def test_draft_full_coverage_frozen_replay_and_shared_budget(checker):
    p, build, calls = checker
    d = build()
    b = draft_input(p)
    m = b["evaluation"]["context"]["merchant_id"]
    r = d.evaluate(b, m, "draft1")
    assert r["disposition"] == "pass" and not r["repair_allowed"]
    assert r["semantic_bundle"]["coverage"]["claim_ids"] == [
        "subject",
        "preheader",
        "body",
        "document",
    ]
    assert len(r["semantic_bundle"]["judgments"]) == 7
    assert build().evaluate(b, m, "draft1") == r
    assert len(calls) == 1
    b["draft"]["revision"] = 1
    b["draft"]["preheader"] = "More delivery information"
    b["draft"]["content_sha256"] = digest(
        {k: b["draft"][k] for k in ("subject", "preheader", "body")}
    )
    d.evaluate(b, m, "repair")
    with psycopg.connect(d.journal.dsn) as db:
        rows = db.execute(
            "SELECT identity FROM af6_budgets WHERE environment=%s AND scope='message'",
            (d.journal.environment,),
        ).fetchall()
    assert len(rows) == 1


@pytest.mark.parametrize(
    "task,answer,expected,repair",
    [
        ("TF06", {"type": "noul", "noul": 0.9}, "fail", True),
        ("TF06", {"type": "noul", "noul": 0.5}, "requires_review", False),
        (
            "TF05",
            {
                "type": "choice",
                "choice": "missing_evidence",
                "confidence": 0.99,
                "probabilities": {
                    "supported": 0,
                    "contradicted": 0,
                    "missing_evidence": 1,
                    "out_of_scope": 0,
                },
            },
            "fail",
            True,
        ),
        (
            "TF07",
            {
                "type": "choice",
                "choice": "insufficient",
                "confidence": 0.99,
                "probabilities": {
                    "consistent": 0,
                    "conflicts_prior_commitment": 0,
                    "insufficient": 1,
                },
            },
            "requires_review",
            False,
        ),
    ],
)
def test_mandatory_failure_cannot_be_averaged(checker, task, answer, expected, repair):
    p, build, calls = checker
    d = build(lambda r: r["answers"].__setitem__(task + ":document", answer))
    b = draft_input(p)
    m = b["evaluation"]["context"]["merchant_id"]
    r = d.evaluate(b, m, "first")
    assert (r["disposition"], r["repair_allowed"]) == (expected, repair)
    assert any(
        c.startswith(task + ":document: ") and len(c) > 60
        for c in r["failure_constraints"]
    )
    assert d.evaluate(b, m, "first") == r and len(calls) == 1
    b["draft"]["revision"] = 1
    assert not d.evaluate(b, m, "repair")["repair_allowed"]


def test_unannotated_claims_are_still_checked(checker):
    p, build, calls = checker
    d = build()
    b = draft_input(p)
    b["draft"]["claims"] = []
    d.evaluate(b, b["evaluation"]["context"]["merchant_id"], "unannotated")
    assert (
        "TF05:document" in calls[0]["questions"]
        and "TF05:body" in calls[0]["questions"]
    )
    assert calls[0]["state"]["surfaces"]["body"] == b["draft"]["body"]
    assert set(calls[0]["state"]["approved_facts"]) == set(
        b["plan"]["decision"]["fact_ids"]
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b["draft"]["claims"][0].update(source_quote="invented quote"),
        lambda b: b["draft"].update(content_sha256="f" * 64),
        lambda b: b["draft"].update(revision=2),
        lambda b: b["plan"]["binding"].update(snapshot_sha256="e" * 64),
        lambda b: b["draft"]["claims"][0].update(fact_id="foreign"),
        lambda b: b["draft"].update(planning_basis="explicit_report"),
    ],
)
def test_literal_binding_guards_precede_inference(checker, change):
    p, build, calls = checker
    d = build()
    b = draft_input(p)
    change(b)
    with pytest.raises(ValueError):
        d.evaluate(b, b["evaluation"]["context"]["merchant_id"], "bad")
    assert not calls


def test_incomplete_or_malformed_cannot_pass(checker):
    p, build, calls = checker
    d = build(lambda r: r["answers"]["TF06:document"].update(confidence=0.99))
    b = draft_input(p)
    r = d.evaluate(b, b["evaluation"]["context"]["merchant_id"], "malformed")
    assert r["disposition"] == "requires_review" and not r["repair_allowed"]


def test_noul_is_probability_only():
    req = {
        "state": {},
        "questions": {"risk": {"type": "noul", "instructions": "Risk?"}},
    }
    strict_response(
        req,
        {
            "model": "typesafe/jev-1.13",
            "answers": {"risk": {"type": "noul", "noul": 0.02}},
        },
        ["typesafe/jev-1.13"],
    )


def test_signed_http_route_and_off_by_default(checker, monkeypatch):
    import time

    p, build, calls = checker
    app = Flask(__name__)
    app.register_blueprint(bp)
    app.extensions["adaptive_draft_checker"] = build()
    client = app.test_client()
    assert client.post(PATH).status_code == 503
    monkeypatch.setenv("ADAPTIVE_FOLLOWUP_DRAFT_ENABLED", "true")
    monkeypatch.setenv("WAKARU_API_KEY", "key")
    monkeypatch.setenv("WAKARU_INTERNAL_SECRET", "secret")
    b = draft_input(p)
    raw = json.dumps(b).encode()
    m = b["evaluation"]["context"]["merchant_id"]
    ts = str(int(time.time()))
    headers = {
        "X-API-Key": "key",
        "X-Merchant-Id": m,
        "Idempotency-Key": "draft",
        "X-Wakaru-Timestamp": ts,
        "X-Wakaru-Signature": signature("secret", ts, m, "draft", raw),
    }
    assert client.post(PATH, data=raw, headers=headers).status_code == 200
    headers["Idempotency-Key"] = "changed"
    assert client.post(PATH, data=raw, headers=headers).status_code == 401
