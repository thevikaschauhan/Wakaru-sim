"""RR06 file workflow and adversarial evidence checks. No network or database."""

import copy
import json
import socket

import pytest

from cart_recovery.adaptive.labeling import (
    cli,
    fixtures,
    metrics,
    pack,
    replay,
    reviews,
)
from cart_recovery.adaptive import draft, planning


@pytest.fixture
def frozen():
    return pack.freeze(
        fixtures.cases(),
        [["synthetic-commerce", "en"], ["synthetic-commerce", "fr"]],
        "a" * 64,
        frozen_at=fixtures.FROZEN_AT,
    )


def test_rr06_file_workflow_and_task_ablation_cannot_authorize(
    frozen, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        socket.socket,
        "connect",
        lambda *_: pytest.fail("offline workflow attempted network"),
    )
    submissions, adjudication = fixtures.reviews(frozen)
    records = fixtures.records(frozen)
    paths = {
        name: tmp_path / (name + ".json")
        for name in ("pack", "submissions", "resolutions", "predictions", "report")
    }
    for name, value in (
        ("pack", frozen),
        ("submissions", submissions),
        ("resolutions", {}),
        ("predictions", records),
    ):
        pack.write_new(paths[name], value)
    assert (
        cli.main(
            [
                "analyze",
                *[
                    arg
                    for name in ("pack", "submissions", "resolutions", "predictions")
                    for arg in ("--" + name, str(paths[name]))
                ],
                "--output",
                str(paths["report"]),
            ]
        )
        == 0
    )
    report = pack.load(paths["report"])
    assert (
        not report["production_qualified"]
        and not report["model_quality_proven"]
        and not report["enablement_authorized"]
    )
    assert set(report["representative"]) == set(replay.VARIANTS)
    assert len(report["task_variant_safety"]) == 7 * len(replay.VARIANTS)
    assert report["safety"]["full"]["hazard_false_pass_journeys"] == 0
    assert report["safety"]["without_TF06"]["hazard_false_pass_journeys"] > 0
    out = report["ablation_outputs"]["safety:0"]
    assert (
        out["without_TF06"]["counterfactual_send_ready"]
        and not out["without_TF06"]["full_mandatory_draft_pass"]
    )
    assert all(not x["runtime_send_authorized"] for x in out.values())
    assert (
        report["total_reserved_unknown_micros"] > 0
        and report["cost_per_delivered_message"] is None
    )
    assert report["claim_coverage"]["critical_misses"] == 2
    assert (
        report["primitive_metrics"][
            "TF06:draft:test:synthetic-commerce:en:generated_workflow"
        ]["primitive"]
        == "noul"
    )


def test_rr06_frozen_pack_is_deterministic_and_detects_tampering(frozen):
    assert frozen == pack.freeze(
        fixtures.cases(), frozen["scope"], "a" * 64, frozen_at=fixtures.FROZEN_AT
    )
    altered = copy.deepcopy(frozen)
    altered["cases"][0]["evidence"]["draft"]["body"] += "new claim"
    with pytest.raises(ValueError, match="hash mismatch"):
        pack.verify(altered)
    altered = copy.deepcopy(frozen)
    altered["binding"]["selection_policy"]["tf03_minimum"] = 0
    altered["pack_sha256"] = planning.digest(
        {k: v for k, v in altered.items() if k != "pack_sha256"}
    )
    with pytest.raises(ValueError, match="binding changed"):
        pack.verify(altered)


@pytest.mark.parametrize(
    "fault",
    [
        "merchant",
        "journey",
        "chronology",
        "duplicate_content",
        "unsupported_locale",
        "foreign_snapshot",
        "review_evidence",
    ],
)
def test_rr06_split_and_evidence_leakage_rejected(frozen, fault):
    cases = copy.deepcopy(frozen["cases"])
    if fault == "merchant":
        cases[0]["split"] = "test"
    elif fault == "journey":
        cases[4]["journey_id"] = cases[0]["journey_id"]
    elif fault == "chronology":
        cases[4]["split"] = "development"
        for c in cases[5:8]:
            c["split"] = "development"
        cases[0]["split"] = "calibration"
        for c in cases[1:4]:
            c["split"] = "calibration"
    elif fault == "duplicate_content":
        cases[1]["evidence"] = copy.deepcopy(cases[0]["evidence"])
        cases[1]["selection"] = copy.deepcopy(cases[0]["selection"])
        cases[1]["selection"]["context"].update(
            merchant_id=cases[1]["merchant_id"], journey_id=cases[1]["journey_id"]
        )
    elif fault == "unsupported_locale":
        cases[0]["locale"] = "de"
    elif fault == "foreign_snapshot":
        cases[0]["selection"]["context"]["merchant_id"] = cases[1]["journey_id"]
    else:
        cases[0]["evidence"]["facts"] = "different facts"
    with pytest.raises(ValueError):
        pack.freeze(cases, frozen["scope"], "a" * 64, frozen_at=fixtures.FROZEN_AT)


def test_rr06_blinding_omits_policy_predictions_outcomes_and_identity(frozen):
    who = {"id": "reviewer", "kind": "human", "independent_ref": "declared-only"}
    packet, mapping = reviews.blind(frozen, who, b"a" * 32, "test")
    assert {i["item_id"] for i in packet["items"]} == set(mapping["mapping"])
    text = json.dumps(packet)
    for value in [
        frozen["binding"]["selection_policy"]["model"],
        frozen["cases"][8]["merchant_id"],
        '"acceptable_actions"',
        '"outcomes"',
        '"predictions"',
        '"hazardous"',
    ]:
        assert value not in text
    other, _ = reviews.blind(frozen, {**who, "id": "another"}, b"a" * 32, "test")
    assert {i["item_id"] for i in packet["items"]}.isdisjoint(
        {i["item_id"] for i in other["items"]}
    )


def test_rr06_real_holdout_stays_protected_without_unblinding_authority(frozen):
    real = pack.freeze(
        frozen["cases"],
        frozen["scope"],
        "a" * 64,
        provenance="authorized_deidentified",
        frozen_at=fixtures.FROZEN_AT,
    )
    with pytest.raises(ValueError, match="separately authorized"):
        reviews.blind(
            real,
            {"id": "human", "kind": "human", "independent_ref": "ref"},
            b"a" * 32,
            "safety",
        )


def test_rr06_two_reviewers_and_third_adjudicator_are_required(frozen):
    submissions, _ = fixtures.reviews(frozen)
    missing = reviews.adjudicate(frozen, submissions[:4], {})
    assert len(missing["unresolved"]) == len(frozen["cases"])
    b = copy.deepcopy(submissions)
    changed = b[4]
    ident = next(iter(changed["labels"]))
    changed["labels"][ident]["repetitive"] = not changed["labels"][ident]["repetitive"]
    changed["submission_sha256"] = planning.digest(
        {k: v for k, v in changed.items() if k != "submission_sha256"}
    )
    unresolved = reviews.adjudicate(frozen, b, {})
    assert ident in unresolved["unresolved"]
    refs = sorted(s["submission_sha256"] for s in b if ident in s["labels"])
    resolution = {
        "reviewer": {
            "id": "third",
            "kind": "fixture_human",
            "independent_ref": "fixture",
        },
        "input_submissions": refs,
        "label": fixtures.gold(
            next(c for c in frozen["cases"] if c["case_id"] == ident)
        ),
        "resolved_at": "2026-10-31T02:00:00Z",
    }
    done = reviews.adjudicate(frozen, b, {ident: resolution})
    assert not done["unresolved"] and done["audit"][ident]["disagreement"]
    resolution["reviewer"]["id"] = b[0]["reviewer"]["id"]
    with pytest.raises(ValueError, match="third reviewer"):
        reviews.adjudicate(frozen, b, {ident: resolution})
    with pytest.raises(ValueError, match="twice"):
        reviews.adjudicate(frozen, submissions + submissions[:1], {})


def test_rr06_model_cannot_be_a_reviewer_or_silent_gold(frozen):
    who = {"id": "model", "kind": "model", "independent_ref": "composer"}
    with pytest.raises(ValueError, match="human reviewer"):
        reviews.blind(frozen, who, b"a" * 32, "test")
    sub, _ = fixtures.reviews(frozen)
    sub[0]["reviewer"]["kind"] = "model"
    sub[0]["submission_sha256"] = planning.digest(
        {k: v for k, v in sub[0].items() if k != "submission_sha256"}
    )
    with pytest.raises(ValueError, match="invalid independent"):
        reviews.adjudicate(frozen, sub, {})


def test_rr06_primitive_metrics_follow_probability_and_ordinal_semantics():
    q = {"type": "choice", "criteria": {"a": "A", "b": "B", "none": "Unknown"}}
    a = {
        "type": "choice",
        "choice": "a",
        "confidence": 1,
        "probabilities": {"a": 1, "b": 0, "none": 0},
    }
    m = metrics.primitive(q, [(a, "b"), (None, "b")])
    assert (
        m["multiclass_brier"] == 2
        and m["selective_risk"][-1]["error_rate"] == 1
        and m["answer_coverage"] == 0.5
    )
    n = metrics.primitive(
        {"type": "noul"}, [({"noul": 0.9}, False), ({"noul": 0.1}, True)]
    )
    assert n["brier"] == pytest.approx(0.81) and "confidence" not in n
    s = metrics.primitive(
        {"type": "score", "criteria": planning.LEVELS},
        [
            (
                {
                    "score": 2.5,
                    "confidence": 0.5,
                    "probabilities": {"0": 0, "1": 0, "2": 0.5, "3": 0.5},
                },
                0,
            )
        ],
    )
    assert s["ordinal_mae"] == 2.5 and s["ranked_probability_score"] == 0.75


@pytest.mark.parametrize(
    "fault",
    [
        "noul_confidence",
        "distribution",
        "score",
        "missing_answer",
        "foreign_binding",
        "missing_record",
        "duplicate_record",
        "unknown_cost_zero",
        "missing_stage",
        "span_past_end",
        "time",
    ],
)
def test_rr06_malformed_or_unpaired_predictions_fail_closed(frozen, fault):
    records = fixtures.records(frozen)
    r = records[0]
    if fault == "noul_confidence":
        r["draft"]["answers"]["TF06:document"]["confidence"] = 0.99
    elif fault == "distribution":
        r["selection"]["answers"]["TF01:delta"]["probabilities"]["useful_change"] = 0.5
    elif fault == "score":
        r["selection"]["answers"]["TF03:shipping"]["score"] = 1
    elif fault == "missing_answer":
        del r["draft"]["answers"]["TF05:document"]
    elif fault == "foreign_binding":
        r["pack_sha256"] = "b" * 64
    elif fault == "missing_record":
        records.pop()
    elif fault == "duplicate_record":
        records.append(records[0])
    elif fault == "unknown_cost_zero":
        r["attempts"][0].update(actual_micros=None, reserved_unknown_micros=0)
    elif fault == "missing_stage":
        r["attempts"] = [r["attempts"][0]]
    elif fault == "span_past_end":
        r["checked_spans"][0]["end"] = 100000
    else:
        r["recorded_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        replay.replay(frozen, records)


def test_rr06_missing_reviews_and_always_abstain_remain_in_denominator(frozen):
    no_labels = reviews.adjudicate(frozen, [], {})
    records = fixtures.records(frozen)
    for r in records:
        r.update(selection=None, draft=None, checked_spans=[])
    report = metrics.analyze(frozen, no_labels, records)
    assert report["representative"]["full"]["useful_action_rate"] == 0
    assert report["representative"]["full"]["automatic_coverage"] == 0
    assert report["representative"]["full"]["review_burden"] == 1
    assert report["representative"]["full"]["independent_journeys"] == 4
    assert report["safety"]["full"]["zero_failure_one_sided_95_upper"] is None
    assert report["safety"]["full"]["adjudicated_dangerous_journeys"] == 0
    assert report["safety"]["full"]["hazard_gold_unknown_journeys"] == 4
    assert "independent_reviews_or_adjudication_incomplete" in report["blocked_gates"]


def test_rr06_journey_variants_do_not_inflate_safety_bound(frozen):
    cases = copy.deepcopy(frozen["cases"])
    variant = copy.deepcopy(cases[-4])
    variant["case_id"] += ":paraphrase"
    variant["evidence"]["draft"]["body"] += " Another wording."
    cases.append(variant)
    p = pack.freeze(cases, frozen["scope"], "a" * 64, frozen_at=fixtures.FROZEN_AT)
    # fixture builders use the base numeric suffix, so reuse the bound record and labels.
    rs = fixtures.records(frozen)
    for r in rs:
        r["pack_sha256"] = p["pack_sha256"]
    rs.append({**copy.deepcopy(rs[-4]), "case_id": variant["case_id"]})
    for a in rs[-1]["attempts"]:
        a["attempt_id"] += ":paraphrase"
    result = replay.replay(p, rs)
    labels = {c["case_id"]: fixtures.gold(c) for c in frozen["cases"]}
    labels[variant["case_id"]] = labels[cases[-5]["case_id"]]
    m = metrics.outcomes([c for c in cases if c["split"] == "safety"], labels, result)
    assert m["full"]["case_n"] == 5 and m["full"]["independent_journeys"] == 4
    assert m["full"]["zero_failure_one_sided_95_upper"] == pytest.approx(1 - 0.05**0.25)


def test_rr06_full_release_keeps_all_mandatory_checks(frozen):
    r = fixtures.records(frozen)[0]
    c = frozen["cases"][0]
    for key in draft.questions():
        altered = copy.deepcopy(r)
        if key.startswith("TF06"):
            altered["draft"]["answers"][key]["noul"] = 0.5
        else:
            altered["draft"]["answers"][key]["confidence"] = 0
        replay.validate_record(frozen, c, altered)
        out = replay.variants(c, altered)
        assert not out["full"]["counterfactual_send_ready"]
        assert all(not x["full_mandatory_draft_pass"] for x in out.values())


def test_rr06_duplicate_json_key_and_output_overwrite_rejected(tmp_path, frozen):
    bad = tmp_path / "bad.json"
    bad.write_text('{"labels":1,"labels":2}')
    with pytest.raises(ValueError, match="duplicate JSON"):
        pack.load(bad)
    pack.write_new(tmp_path / "new.json", frozen)
    with pytest.raises(FileExistsError):
        pack.write_new(tmp_path / "new.json", frozen)


def test_rr06_missing_surface_coverage_holds_full_workflow(frozen):
    record = fixtures.records(frozen)[0]
    record["checked_spans"] = record["checked_spans"][:1]
    result = replay.variants(frozen["cases"][0], record)
    assert not result["full"]["counterfactual_send_ready"]
    assert all(not v["full_text_coverage"] for v in result.values())


def test_rr06_unsafe_primitive_is_visible_when_another_checker_blocks(frozen):
    _, adjudication = fixtures.reviews(frozen)
    report = metrics.analyze(frozen, adjudication, fixtures.records(frozen))
    assert report["safety"]["full"]["hazard_false_pass_journeys"] == 0
    assert report["raw_task_safety"]["TF05"]["false_pass_journeys"] > 0
    assert "critical_task_false_passes:TF05" in report["blocked_gates"]


def test_rr06_private_output_is_restricted_from_creation(tmp_path, monkeypatch):
    import io
    import os

    opened_modes = []
    original_open = io.open

    def observe_creation(*args, **kwargs):
        stream = original_open(*args, **kwargs)
        opened_modes.append(os.fstat(stream.fileno()).st_mode & 0o777)
        return stream

    monkeypatch.setattr(io, "open", observe_creation)
    previous = os.umask(0o022)
    try:
        output = tmp_path / "private-review.json"
        pack.write_new(output, {"evidence": "private review material"})
        assert opened_modes == [0o600]
        assert pack.load(output) == {"evidence": "private review material"}
    finally:
        os.umask(previous)


@pytest.mark.parametrize("kind", ["human", "fixture_human"])
def test_rr06_real_corpus_adjudication_requires_human_reviewer(frozen, kind):
    case = frozen["cases"][0]
    real = pack.freeze(
        [case],
        frozen["scope"],
        frozen["candidate_sha256"],
        provenance="authorized_deidentified",
        frozen_at=fixtures.FROZEN_AT,
    )
    submissions = []
    gold = fixtures.gold(case)
    for index in range(2):
        packet, mapping = reviews.blind(
            real,
            {
                "id": "reviewer-" + str(index),
                "kind": "human",
                "independent_ref": "declared:independent-review-" + str(index),
            },
            b"test-private-blinding-key-000000000",
            "development",
        )
        label = copy.deepcopy(gold)
        if index:
            label["repetitive"] = not label["repetitive"]
        submissions.append(
            reviews.submit(
                real,
                packet,
                mapping,
                {token: label for token in mapping["mapping"]},
                "2026-10-31T01:00:00Z",
            )
        )
    resolutions = {
        case["case_id"]: {
            "reviewer": {
                "id": "adjudicator",
                "kind": kind,
                "independent_ref": "declared:independent-adjudication",
            },
            "input_submissions": [s["submission_sha256"] for s in submissions],
            "label": gold,
            "resolved_at": "2026-10-31T02:00:00Z",
        }
    }
    if kind == "fixture_human":
        with pytest.raises(ValueError, match="fixture reviewers"):
            reviews.adjudicate(real, submissions, resolutions)
    else:
        result = reviews.adjudicate(real, submissions, resolutions)
        assert result["labels"][case["case_id"]] == gold
        assert result["unresolved"] == []
        assert result["independence_authenticated"] is False
