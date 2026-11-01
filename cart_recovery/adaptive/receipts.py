"""AF01 envelopes around the retained OpenRouter transport.

The reference state binds the full resolved provider state transitively. The
journal retains that exact state, so neither source text nor criteria can change
behind a cached result. Costs are batch-level in diagnostics, not multiplied by
number of questions. Usage fields without provider measurements are explicitly
marked unreported; their zero placeholders are never used for budget admission.
"""

from uuid import uuid5, NAMESPACE_URL
from .planning import digest, contracts, CATALOG


def envelope(body, binding, provider_request, key, policy):
    c = body["context"]
    state = {
        "buyer_state_ref": c["buyer_state_ref"],
        "delta_refs": [
            {
                "id": "resolved-provider-state",
                "revision": body["message_revision"],
                "sha256": digest(provider_request["state"]),
            },
            c["cart_ref"],
        ],
        "conversation_refs": [
            {
                "id": "confirmed-conversation",
                "revision": c["conversation_revision"],
                "sha256": digest(c["conversation"]),
            }
        ],
        "fact_refs": c["fact_refs"],
        "spans": [
            {
                "id": s["id"],
                "source_ref": body["sources"][s["source_id"]]["ref"],
                "text": s["text"],
                "attribution": {
                    "behavior": "observed_behavior",
                    "brand": "approved_fact",
                    "shopper_report": "shopper_report",
                }[s["attribution"]],
                "occurred_at": s["occurred_at"],
                "received_at": s["received_at"],
                "scope": s["scope"],
            }
            for s in body["spans"]
        ],
        "eligible_purpose_ids": c["eligible_purpose_ids"],
        "allowed_window_ids": [w["id"] for w in c["allowed_windows"]],
        "excluded_purpose_ids": [],
        "coverage": c["coverage"],
    }
    q = []
    for ident, value in provider_request["questions"].items():
        task, subject = ident.split(":", 1)
        q.append(
            {
                "instance_id": ident,
                "task_id": task,
                "type": value["type"],
                "instructions": value["instructions"],
                "state_paths": [
                    "delta_refs",
                    "conversation_refs",
                    "fact_refs",
                    "spans",
                ],
                "options": CATALOG[task]["options"],
                "required": task == "TF01",
                "applicability": "required" if task == "TF01" else "conditional",
                "applicability_rule_revision": "af06-applicability-v1",
                "subject_id": subject,
            }
        )
    request = {
        "schema_version": "followup_judgment_request_v1",
        "binding": binding,
        "idempotency_key": key,
        "semantic_input_sha256": digest(state),
        "state": state,
        "questions": q,
        "deadline_ms": policy["deadline_ms"],
        "max_input_bytes": policy["max_input_bytes"],
        "max_cost_micros": policy["limits_micros"]["batch"],
        "tools_allowed": False,
    }
    contracts.validate("judgment-request.schema.json", request)
    return request


def disposition(task, a, policy):
    if a["confidence"] < policy[task.lower() + "_confidence"]:
        return "requires_review"
    if task == "TF03":
        return "pass" if a["value"] >= policy["tf03_minimum"] else "fail"
    good = {
        "TF01": ["useful_change", "no_useful_change"],
        "TF02": ["explicit_report", "behavior_only"],
        "TF04": ["adds_useful_information", "answers_prior_question"],
    }
    if a["choice"] in good[task]:
        return "pass"
    return (
        "requires_review" if a["choice"] in ["insufficient", "conflicting"] else "fail"
    )


def bundle(request, raw, consumed, decision, policy, body):
    judgments = []
    for q in request["questions"]:
        ident, task = q["instance_id"], q["task_id"]
        j = {
            "schema_version": "followup_judgment_result_v1",
            "judgment_id": str(uuid5(NAMESPACE_URL, digest(request) + ":" + ident)),
            "instance_id": ident,
            "task_id": task,
            "binding": request["binding"],
            "request_sha256": digest(request),
            "semantic_input_sha256": request["semantic_input_sha256"],
            "input_evidence_refs": request["state"]["fact_refs"]
            + request["state"]["delta_refs"]
            + request["state"]["conversation_refs"]
            + [s["source_ref"] for s in request["state"]["spans"]],
            "service_status": raw["status"],
            "disposition": "requires_review",
            "reason_codes": ["batch_usage_in_journal", "token_usage_unreported"],
            "usage": {
                "latency_ms": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_micros": 0,
                "cache_hit": raw.get("cache_hit", False),
            },
        }
        if raw["status"] == "evaluated":
            answer = dict(raw["response"]["answers"][ident])
            if task == "TF03":
                answer["value"] = answer.pop("score")
                answer["legend"] = ["0", "1", "2", "3"]
            j.update(
                answer=answer,
                disposition=disposition(task, answer, policy),
                provider_request_id=raw.get("response", {}).get("id")
                or raw["request_key"],
                response_version="openrouter-v1",
            )
            if task == "TF02" and answer["choice"] == "explicit_report":
                purpose = next(
                    p for p in body["purposes"] if p["id"] == q["subject_id"]
                )
                if not purpose["reported_span_id"]:
                    j["disposition"] = "requires_review"
                    j["reason_codes"].append("explicit_report_span_missing")
        else:
            j["error_code"] = raw.get("error_class") or "semantic_unavailable"
        contracts.validate("judgment-result.schema.json", j)
        judgments.append(j)
    # Only the selected branch supports an automatic message. Speculative
    # losing branches remain available as unused diagnostics, including failures.
    completed = [j["instance_id"] for j in judgments]
    out = {
        "schema_version": "followup_semantic_bundle_v1",
        "binding": request["binding"],
        "request_sha256": digest(request),
        "semantic_input_sha256": request["semantic_input_sha256"],
        "expected_instance_ids": completed,
        "completed_instance_ids": completed,
        "consumed_instance_ids": consumed,
        "unused_instance_ids": sorted(set(completed) - set(consumed)),
        "judgments": judgments,
        "coverage": request["state"]["coverage"],
        "disposition": "pass" if decision["action"] == "message" else "requires_review",
        "reason_codes": ["planning_recommendation_only"],
    }
    contracts.verify_bundle(request, out)
    return out
