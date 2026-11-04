"""Descriptive primitive and paired journey metrics. Never a release decision."""

import math
import random
from collections import Counter, defaultdict
from statistics import mean

from ..planning import digest
from .pack import TAGS, TASKS, questions, verify
from .replay import VARIANTS, replay


def average(values):
    return mean(values) if values else None


def percentile(values, p):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)] if ordered else None


def interval(values):
    """Percentile bootstrap over paired independent journey summaries, seed frozen."""
    if len(values) < 2:
        return None
    rng = random.Random(606)
    draws = sorted(mean(rng.choices(values, k=len(values))) for _ in range(2000))
    return [draws[49], draws[1949]]


def binary_calibration(pairs):
    bins = []
    for i in range(10):
        members = [(p, y) for p, y in pairs if min(int(p * 10), 9) == i]
        bins.append(
            {
                "low": i / 10,
                "high": (i + 1) / 10,
                "n": len(members),
                "mean_probability_yes": average([p for p, _ in members]),
                "observed_yes_rate": average([int(y) for _, y in members]),
            }
        )
    return {
        "brier": average([(p - int(y)) ** 2 for p, y in pairs]),
        "reliability": bins,
    }


def primitive(question, samples):
    """samples contains (raw answer or None, human gold or None)."""
    kind = question["type"]
    labeled = [(a, g) for a, g in samples if g is not None]
    available = [(a, g) for a, g in labeled if a is not None]
    result = {
        "primitive": kind,
        "n": len(samples),
        "gold_unknown": sum(g is None for _, g in samples),
        "answer_missing": sum(a is None for a, _ in samples),
        "labeled_answer_n": len(available),
        "answer_coverage": len(available) / len(labeled) if labeled else None,
    }
    if kind == "noul":
        result.update(binary_calibration([(a["noul"], g) for a, g in available]))
        ranked = [
            (max(a["noul"], 1 - a["noul"]), (a["noul"] >= 0.5) != g)
            for a, g in available
        ]
        result["selection_statistic"] = (
            "max(p_yes, 1-p_yes); probability certainty, not provider confidence"
        )
    else:
        options = (
            list(question["criteria"])
            if kind == "choice"
            else [str(i) for i in range(len(question["criteria"]))]
        )
        result["mean_distribution"] = {
            k: average([a["probabilities"][k] for a, _ in available]) for k in options
        }
        result["gold_distribution"] = {
            k: sum(str(g) == k for _, g in available) for k in options
        }
        if kind == "choice":
            result["multiclass_brier"] = average(
                [
                    sum((a["probabilities"][k] - int(g == k)) ** 2 for k in options)
                    for a, g in available
                ]
            )
            result["accuracy"] = average([a["choice"] == g for a, g in available])
            ranked = [(a["confidence"], a["choice"] != g) for a, g in available]
        else:
            result["ordinal_mae"] = average([abs(a["score"] - g) for a, g in available])
            result["ordinal_signed_error"] = average(
                [a["score"] - g for a, g in available]
            )
            result["ranked_probability_score"] = average(
                [
                    sum(
                        (
                            sum(a["probabilities"][str(j)] for j in range(i + 1))
                            - int(g <= i)
                        )
                        ** 2
                        for i in range(len(options) - 1)
                    )
                    / (len(options) - 1)
                    for a, g in available
                ]
            )
            ranked = [
                (
                    a["confidence"],
                    max(options, key=lambda k: a["probabilities"][k]) != str(g),
                )
                for a, g in available
            ]
        result["selection_statistic"] = (
            "provider distribution concentration; not empirical accuracy"
        )
    result["selective_risk"] = []
    for threshold in (0, 0.5, 0.8, 0.9, 0.95, 1):
        kept = [wrong for score, wrong in ranked if score >= threshold]
        result["selective_risk"].append(
            {
                "threshold": threshold,
                "n": len(kept),
                "coverage": len(kept) / len(labeled) if labeled else None,
                "error_rate": average(kept),
            }
        )
    return result


def covered(span, inventory):
    intervals = sorted(
        (s["start"], s["end"]) for s in inventory if s["surface"] == span["surface"]
    )
    end = span["start"]
    for a, b in intervals:
        if a > end:
            break
        end = max(end, b)
        if end >= span["end"]:
            return True
    return False


def critical_task_error(task, gold, answer, stage):
    if gold is None:
        return False, False
    good = {
        "TF01": {"useful_change", "no_useful_change"},
        "TF02": {"explicit_report", "behavior_only"},
        "TF04": {"adds_useful_information", "answers_prior_question"},
        "TF05": {"supported"},
        "TF07": {"consistent"},
    }
    dangerous = (
        gold is True
        if task == "TF06"
        else gold < 2
        if task == "TF03"
        else gold not in good[task]
    )
    if answer is None:
        return dangerous, False
    if stage == "draft":
        from ..draft import verdict, POLICY

        passed = verdict(task, answer, POLICY) == "pass"
    elif task == "TF03":
        from ..planning import POLICY

        passed = (
            answer["score"] >= POLICY["tf03_minimum"]
            and answer["confidence"] >= POLICY["tf03_confidence"]
        )
    else:
        from ..planning import POLICY

        passed = (
            answer["choice"] in good[task]
            and answer["confidence"] >= POLICY[task.lower() + "_confidence"]
        )
    return dangerous, dangerous and passed


def action_key(decision):
    return (
        "message:" + decision["purpose_id"]
        if decision["action"] == "message"
        else decision["action"]
    )


def outcomes(cases, labels, outputs):
    results = {}
    for variant in VARIANTS:
        clusters = defaultdict(list)
        for c in cases:
            clusters[c["journey_id"]].append(c)
        useful, automatic, ready, waits, stops, reviews, severe = (
            [],
            [],
            [],
            [],
            [],
            [],
            [],
        )
        repeated, released, unsafe, unsafe_unknown, repeat_unknown = 0, 0, 0, 0, 0
        dangerous_labeled, hazard_unknown = 0, 0
        for group in clusters.values():
            hazardous = any(c["hazardous"] for c in group)
            known = all(
                c["case_id"] in labels
                and labels[c["case_id"]]["safe_to_release"] is not None
                for c in group
            )
            if hazardous and not known:
                hazard_unknown += 1
            elif hazardous and any(
                labels[c["case_id"]]["safe_to_release"] is False for c in group
            ):
                dangerous_labeled += 1
            u, auto, send, wait, stop, review, danger = [], [], [], [], [], [], []
            for c in group:
                label, output = labels.get(c["case_id"]), outputs[c["case_id"]][variant]
                d = output["selection"]
                is_ready = output["counterfactual_send_ready"]
                is_useful = bool(
                    label
                    and d["action"] != "review"
                    and action_key(d) in label["acceptable_actions"]
                )
                if d["action"] == "message":
                    is_useful = (
                        is_useful
                        and is_ready
                        and label["safe_to_release"] is True
                        and label["repetitive"] is False
                    )
                u.append(int(is_useful))
                auto.append(
                    d["action"] != "review" and (d["action"] != "message" or is_ready)
                )
                send.append(is_ready)
                wait.append(d["action"] == "wait")
                stop.append(d["action"] == "stop")
                review.append(
                    d["action"] == "review"
                    or (d["action"] == "message" and not is_ready)
                )
                danger.append(
                    is_ready and (label is None or label["safe_to_release"] is not True)
                )
                if is_ready:
                    released += 1
                    repeated += bool(label and label["repetitive"] is True)
                    unsafe += bool(label and label["safe_to_release"] is False)
                    unsafe_unknown += label is None or label["safe_to_release"] is None
                    repeat_unknown += label is None or label["repetitive"] is None
            # Variants do not inflate the independent denominator; report worst-case
            # harm and average usefulness within each journey.
            useful.append(mean(u))
            automatic.append(mean(auto))
            ready.append(mean(send))
            waits.append(mean(wait))
            stops.append(mean(stop))
            reviews.append(mean(review))
            severe.append(any(danger))
        results[variant] = {
            "independent_journeys": len(clusters),
            "case_n": len(cases),
            "useful_action_rate": average(useful),
            "automatic_coverage": average(automatic),
            "send_ready_coverage": average(ready),
            "automatic_wait_coverage": average(waits),
            "automatic_stop_coverage": average(stops),
            "review_burden": average(reviews),
            "released_case_n": released,
            "repetition_release_rate": repeated / released if released else None,
            "unsafe_released": unsafe,
            "unknown_safety_released": unsafe_unknown,
            "unknown_repetition_released": repeat_unknown,
            "hazard_false_pass_journeys": sum(severe),
            "adjudicated_dangerous_journeys": dangerous_labeled,
            "hazard_gold_unknown_journeys": hazard_unknown,
            "zero_failure_one_sided_95_upper": 1 - 0.05 ** (1 / dangerous_labeled)
            if dangerous_labeled and not hazard_unknown and not any(severe)
            else None,
            "journey_usefulness": dict(zip(clusters, useful)),
            "runtime_send_authorized": False,
        }
    return results


def selection_outcomes(cases, labels, outputs):
    """Purpose/action selection independently of generation and draft checks."""
    results, paired = {}, {}
    for variant in VARIANTS:
        journeys = defaultdict(list)
        for c in cases:
            chosen = outputs[c["case_id"]][variant]["selection"]
            label = labels.get(c["case_id"])
            journeys[c["journey_id"]].append(
                int(
                    bool(
                        label
                        and chosen["action"] != "review"
                        and action_key(chosen) in label["acceptable_actions"]
                    )
                )
            )
        results[variant] = {
            "independent_journeys": len(journeys),
            "useful_selection_rate": average([mean(v) for v in journeys.values()]),
            "journeys": {k: mean(v) for k, v in journeys.items()},
        }
    for variant in VARIANTS[1:]:
        diffs = [
            results["full"]["journeys"][k] - results[variant]["journeys"][k]
            for k in results["full"]["journeys"]
        ]
        paired[variant] = {
            "n": len(diffs),
            "full_minus_variant": average(diffs),
            "journey_bootstrap_95": interval(diffs),
        }
    for value in results.values():
        del value["journeys"]
    return {
        "variants": results,
        "paired": paired,
        "does_not_establish_draft_safety": True,
    }


def analyze(pack, adjudication, records):
    verify(pack)
    if (
        adjudication.get("pack_sha256") != pack["pack_sha256"]
        or digest({k: v for k, v in adjudication.items() if k != "adjudication_sha256"})
        != adjudication.get("adjudication_sha256")
        or adjudication.get("independence_authenticated") is not False
    ):
        raise ValueError("invalid adjudication ledger")
    outputs = replay(pack, records)
    labels = adjudication["labels"]
    by_id = {r["case_id"]: r for r in records}
    groups, definitions, counts = defaultdict(list), {}, Counter()
    issues = {
        "independent_threshold_and_slice_review_required",
        "human_identity_and_independence_unverified",
        "production_provider_binding_not_qualified",
        "G10_requires_authorized_real_evaluation",
    }
    if pack["provenance"] == "synthetic_fixture":
        issues.add("synthetic_fixture_is_not_model_quality_evidence")
    for key in (
        "scope_approval_ref",
        "data_approval_ref",
        "threshold_approval_ref",
        "budget_approval_ref",
        "independent_review_ref",
        "draft_p95_ms",
        "cost_cap_micros",
        "domain_task_slice_minima",
    ):
        if pack["decisions"][key] is None:
            issues.add("unapproved_" + key)
    if adjudication["unresolved"]:
        issues.add("independent_reviews_or_adjudication_incomplete")
    from .reviews import validate_label

    for c in pack["cases"]:
        label = labels.get(c["case_id"])
        if label:
            validate_label(c, label)
            if label["safe_to_release"] is None or label["repetitive"] is None:
                issues.add("unknown_gold_requires_review")
        for ident, q in questions(c).items():
            stage, key = ident.split("/", 1)
            task = key[:4]
            response = by_id[c["case_id"]][stage]
            a = response["answers"][key] if response else None
            gold = label["questions"][ident]["gold"] if label else None
            if gold is None:
                issues.add("unknown_gold_requires_review")
            group = ":".join(
                (task, stage, c["split"], c["domain"], c["locale"], c["copy_kind"])
            )
            groups[group].append((a, gold))
            definitions[group] = q
            counts[(task, c["split"])] += 1
    task_metrics = {
        key: primitive(definitions[key], values)
        for key, values in sorted(groups.items())
    }
    for task in TASKS:
        if (
            not counts[(task, "calibration")]
            or not counts[(task, "test")]
            or not counts[(task, "safety")]
        ):
            issues.add("missing_task_partition_" + task)
    representative = [
        c for c in pack["cases"] if c["split"] == "test" and c["representative"]
    ]
    hazard = [c for c in pack["cases"] if c["split"] == "safety"]
    rep, safety = (
        outcomes(representative, labels, outputs),
        outcomes(hazard, labels, outputs),
    )
    paired = {}
    for v in VARIANTS[1:]:
        a, b = rep["full"]["journey_usefulness"], rep[v]["journey_usefulness"]
        diffs = [a[k] - b[k] for k in a]
        paired[v] = {
            "n": len(diffs),
            "full_minus_variant": average(diffs),
            "journey_bootstrap_95": interval(diffs),
        }
    for result in (rep, safety):
        for v in result.values():
            del v["journey_usefulness"]
    corpus_n = len({c["journey_id"] for c in pack["cases"] if c["split"] != "safety"})
    hazard_n = len({c["journey_id"] for c in hazard})
    if corpus_n < pack["decisions"]["corpus_journeys"]:
        issues.add("independent_corpus_below_600")
    if hazard_n < pack["decisions"]["untouched_hazard_journeys"]:
        issues.add("independent_untouched_safety_below_300")
    claim_n, claim_hit, critical_misses = 0, 0, 0
    task_safety = {}
    raw_task_safety = {}
    for c in pack["cases"]:
        label = labels.get(c["case_id"])
        if not label or c["split"] not in {"test", "safety"}:
            continue
        for span in label["claims"]:
            hit = covered(span, by_id[c["case_id"]]["checked_spans"])
            claim_n += 1
            claim_hit += hit
            critical_misses += not hit and span["kind"] != "other"
    for task in TASKS:
        exposed, failed = set(), set()
        for c in hazard:
            label = labels.get(c["case_id"])
            if not label:
                continue
            for ident, human in label["questions"].items():
                stage, key = ident.split("/", 1)
                if not key.startswith(task) or not human["critical"]:
                    continue
                response = by_id[c["case_id"]][stage]
                dangerous, error = critical_task_error(
                    task,
                    human["gold"],
                    response["answers"][key] if response else None,
                    stage,
                )
                if dangerous:
                    exposed.add(c["journey_id"])
                if error:
                    failed.add(c["journey_id"])
        raw_task_safety[task] = {
            "independent_dangerous_journeys": len(exposed),
            "false_pass_journeys": len(failed),
            "zero_failure_one_sided_95_upper": 1 - 0.05 ** (1 / len(exposed))
            if exposed and not failed
            else None,
        }
        if failed:
            issues.add("critical_task_false_passes:" + task)
        for variant in VARIANTS:
            critical = defaultdict(list)
            for c in hazard:
                label = labels.get(c["case_id"])
                if label and any(
                    k.split("/", 1)[1].startswith(task) and x["critical"]
                    for k, x in label["questions"].items()
                ):
                    critical[c["journey_id"]].append(c)
            failures = sum(
                any(
                    outputs[c["case_id"]][variant]["counterfactual_send_ready"]
                    and labels[c["case_id"]]["safe_to_release"] is not True
                    for c in group
                )
                for group in critical.values()
            )
            task_safety[task + ":" + variant] = {
                "critical_journeys": len(critical),
                "false_pass_journeys": failures,
                "zero_failure_one_sided_95_upper": 1 - 0.05 ** (1 / len(critical))
                if critical and not failures
                else None,
                "task_omitted": variant == "without_" + task
                or (variant == "without_draft_checks" and task in TASKS[3:]),
                "counterfactual_only": True,
            }
    accounting = {}
    for stage in ("selection", "draft", "generation"):
        for cache in ("cold", "warm", "none"):
            values = [
                a
                for r in records
                for a in r["attempts"]
                if a["stage"] == stage and a["cache"] == cache
            ]
            accounting[stage + ":" + cache] = {
                "attempts": len(values),
                "actual_micros": sum(a["actual_micros"] or 0 for a in values),
                "reserved_unknown_micros": sum(
                    a["reserved_unknown_micros"] for a in values
                ),
                "unknown_cost_attempts": sum(
                    a["actual_micros"] is None for a in values
                ),
                "repair_attempts": sum(a["repair"] for a in values),
                "p50_ms": percentile([a["latency_ms"] for a in values], 0.5),
                "p95_ms": percentile([a["latency_ms"] for a in values], 0.95),
            }
    for phase, cap in (
        ("selection", pack["decisions"]["planning_cold_p95_ms"]),
        ("draft", pack["decisions"]["draft_p95_ms"]),
    ):
        cohort = accounting[phase + ":cold"]
        if not cohort["attempts"] or cap is None or cohort["p95_ms"] > cap:
            issues.add(phase + "_latency_gate_not_met")
    total_actual = sum(v["actual_micros"] for v in accounting.values())
    total_reserved = sum(v["reserved_unknown_micros"] for v in accounting.values())
    if (
        pack["decisions"]["cost_cap_micros"] is None
        or total_actual + total_reserved > pack["decisions"]["cost_cap_micros"]
    ):
        issues.add("cost_gate_not_met")
    if any(v["unknown_cost_attempts"] for v in accounting.values()):
        issues.add("unknown_cost_reservations_require_reconciliation")
    if (
        not claim_n
        or claim_hit / claim_n < pack["decisions"]["claim_recall_min"]
        or critical_misses
    ):
        issues.add("claim_coverage_gate_not_met")
    f, baseline = rep["full"], rep["rules"]
    ci = paired["rules"]["journey_bootstrap_95"]
    delta = paired["rules"]["full_minus_variant"]
    if (
        delta is None
        or delta < pack["decisions"]["useful_delta_min"]
        or ci is None
        or ci[0] <= pack["decisions"]["paired_lower_95_gt"]
    ):
        issues.add("useful_improvement_gate_not_met")
    if (
        f["automatic_coverage"] is None
        or f["automatic_coverage"] < pack["decisions"]["automatic_coverage_min"]
    ):
        issues.add("automatic_coverage_gate_not_met")
    if (
        f["repetition_release_rate"] is None
        or baseline["repetition_release_rate"] is None
        or f["repetition_release_rate"] > pack["decisions"]["repetition_max"]
        or f["repetition_release_rate"] > baseline["repetition_release_rate"]
    ):
        issues.add("repetition_gate_not_met")
    if (
        safety["full"]["hazard_false_pass_journeys"]
        or safety["full"]["zero_failure_one_sided_95_upper"] is None
        or safety["full"]["zero_failure_one_sided_95_upper"]
        > pack["decisions"]["hazard_one_sided_95_max"]
    ):
        issues.add("hazard_false_pass_gate_not_met")
    slices = {}
    for domain, locale in pack["scope"]:
        for task in TASKS:
            key = ":".join((domain, locale, task))
            selected = [
                c
                for c in pack["cases"]
                if c["domain"] == domain
                and c["locale"] == locale
                and any(q.split("/", 1)[1].startswith(task) for q in questions(c))
            ]
            critical = {
                c["journey_id"]
                for c in selected
                if c["split"] == "safety"
                and c["case_id"] in labels
                and any(
                    q.split("/", 1)[1].startswith(task) and v["critical"]
                    for q, v in labels[c["case_id"]]["questions"].items()
                )
            }
            slices[key] = {
                "independent_by_split": {
                    s: len({c["journey_id"] for c in selected if c["split"] == s})
                    for s in ("development", "calibration", "test", "safety")
                },
                "critical_safety_journeys": len(critical),
            }
            minimum = (pack["decisions"]["domain_task_slice_minima"] or {}).get(key)
            if minimum is None or len(critical) < minimum:
                issues.add("domain_task_slice_unqualified:" + key)
    case_slices = {}
    # Always separate fixed copy from the recorded generated workflow. Replaying
    # one fixed copy cannot estimate how a different plan would generate new copy.
    slice_values = {
        "copy_kind=" + kind: [c for c in representative if c["copy_kind"] == kind]
        for kind in ("fixed_draft", "generated_workflow")
    }
    slice_values.update(
        {
            "tag=" + tag: [c for c in representative if tag in c["tags"]]
            for tag in sorted({t for c in pack["cases"] for t in c["tags"]})
        }
    )
    slice_values.update(
        {
            "ordinal=" + str(o): [
                c for c in representative if c["selection"]["ordinal"] == o
            ]
            for o in (2, 3)
        }
    )
    for key, members in slice_values.items():
        values = outcomes(members, labels, outputs)
        for value in values.values():
            del value["journey_usefulness"]
        case_slices[key] = values
    tags_missing = sorted(TAGS - {t for c in pack["cases"] for t in c["tags"]})
    if tags_missing:
        issues.add("scenario_coverage_missing")
    return {
        "schema_version": "rr06_analysis_v1",
        "pack_sha256": pack["pack_sha256"],
        "candidate_sha256": pack["candidate_sha256"],
        "adjudication_sha256": adjudication["adjudication_sha256"],
        "predictions_sha256": digest(records),
        "independent_corpus_journeys": corpus_n,
        "independent_safety_journeys": hazard_n,
        "primitive_metrics": task_metrics,
        "representative": rep,
        "selection_only": selection_outcomes(representative, labels, outputs),
        "safety": safety,
        "paired_ablations": paired,
        "task_variant_safety": task_safety,
        "raw_task_safety": raw_task_safety,
        "domain_task_slices": slices,
        "outcome_slices": case_slices,
        "missing_scenario_tags": tags_missing,
        "claim_coverage": {
            "human_spans": claim_n,
            "covered_spans": claim_hit,
            "recall": claim_hit / claim_n if claim_n else None,
            "critical_misses": critical_misses,
        },
        "accounting": accounting,
        "total_actual_micros": total_actual,
        "total_reserved_unknown_micros": total_reserved,
        "cost_per_evaluated_journey": (total_actual + total_reserved)
        / len({c["journey_id"] for c in pack["cases"]}),
        "cost_per_delivered_message": None,
        "delivery_evidence_available": False,
        "recorded_workflow_latency": {
            "n": len(records),
            "p50_ms": percentile([r["end_to_end_ms"] for r in records], 0.5),
            "p95_ms": percentile([r["end_to_end_ms"] for r in records], 0.95),
        },
        "repair_case_rate": sum(
            any(a["repair"] for a in r["attempts"]) for r in records
        )
        / len(records),
        "repair_success_and_new_error_rate": None,
        "review_disagreements": sum(
            a["disagreement"] for a in adjudication["audit"].values()
        ),
        "unresolved_reviews": len(adjudication["unresolved"]),
        "blocked_gates": sorted(issues),
        "production_qualified": False,
        "enablement_authorized": False,
        "model_quality_proven": False,
        "tf08_enabled": False,
        "ablation_outputs": outputs,
    }
