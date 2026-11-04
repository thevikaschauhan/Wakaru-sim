"""Offline counterfactuals over identical snapshots and recorded answers.

Never imported by runtime handlers. Removing a checker can produce an unsafe
counterfactual, but never changes the full mandatory-gate result or grants send
authority. This is replay, not a claim about regenerating copy under another plan.
"""

import copy

from .. import draft, planning
from .pack import TASKS, exact, instant, number, verify

VARIANTS = (
    "full",
    "rules",
    "without_semantic_selection",
    "without_draft_checks",
) + tuple("without_" + t for t in TASKS)


def validate_record(pack, case, record):
    exact(
        record,
        {
            "pack_sha256",
            "case_id",
            "recorded_at",
            "selection",
            "draft",
            "attempts",
            "checked_spans",
            "end_to_end_ms",
        },
    )
    if (
        record["pack_sha256"] != pack["pack_sha256"]
        or record["case_id"] != case["case_id"]
    ):
        raise ValueError("foreign or unpaired prediction")
    if instant(record["recorded_at"]) < instant(pack["frozen_at"]):
        raise ValueError("prediction precedes freeze")
    number(record["end_to_end_ms"])
    for stage, qs, policy in (
        ("selection", planning.questions(case["selection"]), planning.POLICY),
        ("draft", draft.questions(), draft.POLICY),
    ):
        response = record[stage]
        if response is not None:
            planning.strict_response(
                {"questions": qs}, response, [policy["model"]], policy
            )
    if not isinstance(record["attempts"], list) or not record["attempts"]:
        raise ValueError("attempt accounting required even for unavailable work")
    ids, stages = set(), set()
    for a in record["attempts"]:
        exact(
            a,
            {
                "attempt_id",
                "stage",
                "actual_micros",
                "reserved_unknown_micros",
                "latency_ms",
                "cache",
                "repair",
            },
        )
        if (
            not isinstance(a["attempt_id"], str)
            or not a["attempt_id"]
            or a["attempt_id"] in ids
            or a["stage"] not in {"generation", "selection", "draft"}
            or a["cache"] not in {"cold", "warm", "none"}
            or type(a["repair"]) is not bool
        ):
            raise ValueError("invalid attempt identity/cohort")
        ids.add(a["attempt_id"])
        stages.add(a["stage"])
        number(a["latency_ms"])
        if (
            type(a["reserved_unknown_micros"]) is not int
            or a["reserved_unknown_micros"] < 0
        ):
            raise ValueError("invalid unknown reservation")
        if a["actual_micros"] is None:
            if not a["reserved_unknown_micros"]:
                raise ValueError("unknown cost cannot silently become zero")
        elif (
            type(a["actual_micros"]) is not int
            or a["actual_micros"] < 0
            or a["reserved_unknown_micros"]
        ):
            raise ValueError("settled cost must have no unresolved reservation")
    if not {"selection", "draft"} <= stages or (
        case["copy_kind"] == "generated_workflow" and "generation" not in stages
    ):
        raise ValueError("incomplete stage cost/latency accounting")
    if not isinstance(record["checked_spans"], list):
        raise ValueError("checked span inventory required")
    seen = set()
    for span in record["checked_spans"]:
        exact(span, {"surface", "start", "end"})
        s, start, end = span["surface"], span["start"], span["end"]
        if (
            s not in case["evidence"]["draft"]
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(case["evidence"]["draft"][s].encode())
        ):
            raise ValueError("invalid checked span")
        raw = case["evidence"]["draft"][s].encode()
        raw[:start].decode()
        raw[start:end].decode()
        raw[end:].decode()
        ident = (s, start, end)
        if ident in seen:
            raise ValueError("duplicate checked span")
        seen.add(ident)
        if record["draft"] is None:
            raise ValueError("unavailable checker cannot claim coverage")


def point(question, winner):
    options = list(question["criteria"])
    return {
        "type": "choice",
        "choice": winner,
        "confidence": 1,
        "probabilities": {k: int(k == winner) for k in options},
    }


def selection(case, response, omit=()):
    if response is None or not all(case["evidence"]["coverage"].values()):
        return {"action": "review"}
    body, answers = copy.deepcopy(case["selection"]), copy.deepcopy(response["answers"])
    qs = planning.questions(body)
    for key in answers:
        task = key[:4]
        if task not in omit:
            continue
        if task == "TF01":
            answers[key] = point(qs[key], "no_useful_change")
        elif task == "TF02":
            # Remove concern filtering without inventing an attributable report.
            answers[key] = point(qs[key], "behavior_only")
        elif task == "TF04":
            answers[key] = point(qs[key], "adds_useful_information")
    policy = copy.deepcopy(planning.POLICY)
    if "TF03" in omit:
        # Equal scores would create a ranking tie. Freeze a priority-order baseline
        # for this ablation, retaining TF01/02/04 filters on every candidate.
        uncertain = False
        for p in body["purposes"]:
            single = {**body, "purposes": [p]}
            key = "TF03:" + p["id"]
            answers[key] = {
                "type": "score",
                "score": 3,
                "confidence": 1,
                "probabilities": {str(i): int(i == 3) for i in range(4)},
                "legend": dict(enumerate(planning.LEVELS)),
            }
            decision, _ = planning.reduce_answers(single, answers, policy)
            if decision["action"] == "message":
                return decision
            uncertain = uncertain or decision["action"] == "review"
        return {"action": "review" if uncertain else "wait"}
    return planning.reduce_answers(body, answers, policy)[0]


def variants(case, record):
    # Metrics imports replay; resolve the shared coverage helper after both
    # modules have initialized rather than introducing a top-level cycle.
    from .metrics import covered

    full_checks = (
        {
            key: draft.verdict(key[:4], a, draft.POLICY)
            for key, a in record["draft"]["answers"].items()
        }
        if record["draft"]
        else {key: "requires_review" for key in draft.questions()}
    )
    surface_coverage = all(
        not text
        or covered(
            {"surface": name, "start": 0, "end": len(text.encode())},
            record["checked_spans"],
        )
        for name, text in case["evidence"]["draft"].items()
    )
    full_ok = (
        all(x == "pass" for x in full_checks.values())
        and all(case["evidence"]["coverage"].values())
        and surface_coverage
    )
    out = {}
    for variant in VARIANTS:
        omit = {variant[8:]} if variant.startswith("without_TF") else set()
        if variant in {"rules", "without_semantic_selection"}:
            chosen = (
                planning.rules_decision(case["selection"])
                if all(case["evidence"]["coverage"].values())
                else {"action": "review"}
            )
        else:
            chosen = selection(case, record["selection"], omit)
        omitted_checks = {
            key
            for key in full_checks
            if key[:4] in omit or variant == "without_draft_checks"
        }
        counterfactual_ok = (
            surface_coverage
            and all(
                value == "pass"
                for key, value in full_checks.items()
                if key not in omitted_checks
            )
            and all(case["evidence"]["coverage"].values())
        )
        matched_copy = chosen.get("purpose_id") == case["copy_purpose_id"]
        out[variant] = {
            "selection": chosen,
            "counterfactual_draft_pass": counterfactual_ok,
            "counterfactual_send_ready": chosen["action"] == "message"
            and matched_copy
            and counterfactual_ok,
            "full_mandatory_draft_pass": full_ok,
            "full_text_coverage": surface_coverage,
            "full_checks": full_checks,
            "omitted_checks": sorted(omitted_checks),
            "copy_matches_selection": matched_copy,
            "runtime_send_authorized": False,
        }
    return out


def replay(pack, records):
    verify(pack)
    if not isinstance(records, list) or len({r["case_id"] for r in records}) != len(
        records
    ):
        raise ValueError("duplicate prediction case")
    expected = {c["case_id"]: c for c in pack["cases"]}
    if {r["case_id"] for r in records} != set(expected):
        raise ValueError("predictions must cover the frozen corpus exactly")
    out = {}
    attempts = set()
    for record in records:
        case = expected[record["case_id"]]
        validate_record(pack, case, record)
        for attempt in record["attempts"]:
            if attempt["attempt_id"] in attempts:
                raise ValueError("attempt charged more than once across cases")
            attempts.add(attempt["attempt_id"])
        out[case["case_id"]] = variants(case, record)
    return out
