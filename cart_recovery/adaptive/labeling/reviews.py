"""Blind independent labels, immutable submissions and third-person adjudication.

Identity references are declared evidence, not authenticated attestations. Reports
always require external independence and access review before release use.
"""

import copy
import hmac
from hashlib import sha256

from ..planning import digest
from .pack import KINDS, SPLITS, SURFACES, exact, instant, nonempty, questions, verify


def blind(pack, reviewer, secret, partition):
    verify(pack)
    exact(reviewer, {"id", "kind", "independent_ref"})
    nonempty(reviewer["id"])
    nonempty(reviewer["independent_ref"])
    if reviewer["kind"] not in {"human", "fixture_human"} or partition not in SPLITS:
        raise ValueError("human reviewer and explicit partition required")
    if (
        reviewer["kind"] == "fixture_human"
        and pack["provenance"] != "synthetic_fixture"
    ):
        raise ValueError("fixture reviewers cannot label a real corpus")
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("private blinding key must contain at least 32 bytes")
    # No real safety contents can be exported by this local preparation workflow.
    if pack["provenance"] != "synthetic_fixture" and partition in {"test", "safety"}:
        raise ValueError(
            "protected holdout requires separately authorized unblinding workflow"
        )
    items, mapping = [], {}
    for c in pack["cases"]:
        if c["split"] != partition:
            continue
        token = hmac.new(
            secret,
            (pack["pack_sha256"] + reviewer["id"] + c["case_id"]).encode(),
            sha256,
        ).hexdigest()
        mapping[token] = c["case_id"]
        items.append(
            {
                "item_id": token,
                "domain": c["domain"],
                "locale": c["locale"],
                "evidence": copy.deepcopy(c["evidence"]),
                "questions": questions(c),
            }
        )
    items.sort(key=lambda x: x["item_id"])
    packet = {
        "schema_version": "rr06_blind_packet_v1",
        "pack_sha256": pack["pack_sha256"],
        "reviewer": reviewer,
        "partition": partition,
        "items": items,
    }
    return packet, {"packet_sha256": digest(packet), "mapping": mapping}


def validate_label(case, label):
    exact(
        label,
        {
            "questions",
            "acceptable_actions",
            "safe_to_release",
            "repetitive",
            "claims",
            "rationale",
        },
    )
    expected = questions(case)
    exact(label["questions"], expected)
    for ident, q in expected.items():
        a = label["questions"][ident]
        exact(a, {"gold", "critical"})
        if type(a["critical"]) is not bool:
            raise ValueError("critical label required")
        gold = a["gold"]
        if gold is None:
            continue  # Uncertainty survives adjudication and remains blocked.
        if q["type"] == "noul":
            if type(gold) is not bool:
                raise ValueError("Noul gold must be yes/no")
        elif q["type"] == "score":
            if type(gold) is not int or not 0 <= gold < len(q["criteria"]):
                raise ValueError("Score gold must be an ordered level")
        elif not isinstance(gold, str) or gold not in q["criteria"]:
            raise ValueError("Choice gold must be an exact option")
    valid_actions = {"wait", "review", "stop"} | {
        "message:" + p["id"] for p in case["selection"]["purposes"]
    }
    actions = label["acceptable_actions"]
    if (
        not isinstance(actions, list)
        or not actions
        or len(set(actions)) != len(actions)
        or not set(actions) <= valid_actions
    ):
        raise ValueError("invalid acceptable action set")
    for key in ("safe_to_release", "repetitive"):
        if label[key] is not None and type(label[key]) is not bool:
            raise ValueError("boolean or unknown label required")
    if case["hazardous"] and label["safe_to_release"] is True:
        raise ValueError("hazard holdout requires dangerous or explicitly unknown gold")
    nonempty(label["rationale"])
    if not isinstance(label["claims"], list):
        raise ValueError("claim inventory required, including an explicit empty list")
    seen = set()
    for span in label["claims"]:
        exact(span, {"surface", "start", "end", "kind", "supported"})
        if (
            span["surface"] not in SURFACES
            or span["kind"] not in KINDS
            or type(span["supported"]) is not bool
        ):
            raise ValueError("invalid claim")
        raw = case["evidence"]["draft"][span["surface"]].encode()
        start, end = span["start"], span["end"]
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(raw)
        ):
            raise ValueError("invalid UTF-8 claim span")
        raw[:start].decode()
        raw[start:end].decode()
        raw[end:].decode()
        ident = (span["surface"], start, end)
        if ident in seen:
            raise ValueError("duplicate claim span")
        seen.add(ident)


def submit(pack, packet, mapping, labels, submitted_at):
    verify(pack)
    if (
        mapping["packet_sha256"] != digest(packet)
        or packet["pack_sha256"] != pack["pack_sha256"]
    ):
        raise ValueError("packet or mapping changed")
    exact(labels, mapping["mapping"])
    if {i["item_id"] for i in packet["items"]} != set(mapping["mapping"]):
        raise ValueError("incomplete review packet")
    if instant(submitted_at) < instant(pack["frozen_at"]):
        raise ValueError("review precedes freeze")
    cases = {c["case_id"]: c for c in pack["cases"]}
    out = {}
    for token, ident in mapping["mapping"].items():
        c = cases[ident]
        item = next(i for i in packet["items"] if i["item_id"] == token)
        if (
            c["split"] != packet["partition"]
            or item["evidence"] != c["evidence"]
            or item["questions"] != questions(c)
        ):
            raise ValueError("review case identity changed")
        validate_label(c, labels[token])
        out[ident] = copy.deepcopy(labels[token])
    value = {
        "pack_sha256": pack["pack_sha256"],
        "packet_sha256": digest(packet),
        "reviewer": packet["reviewer"],
        "submitted_at": submitted_at,
        "labels": out,
    }
    return {**value, "submission_sha256": digest(value)}


def adjudicate(pack, submissions, resolutions):
    verify(pack)
    by_case = {c["case_id"]: [] for c in pack["cases"]}
    for sub in submissions:
        exact(
            sub,
            {
                "pack_sha256",
                "packet_sha256",
                "reviewer",
                "submitted_at",
                "labels",
                "submission_sha256",
            },
        )
        if (
            sub["pack_sha256"] != pack["pack_sha256"]
            or digest({k: v for k, v in sub.items() if k != "submission_sha256"})
            != sub["submission_sha256"]
        ):
            raise ValueError("review submission hash mismatch")
        exact(sub["reviewer"], {"id", "kind", "independent_ref"})
        nonempty(sub["reviewer"]["id"])
        nonempty(sub["reviewer"]["independent_ref"])
        if sub["reviewer"]["kind"] not in {"human", "fixture_human"} or instant(
            sub["submitted_at"]
        ) < instant(pack["frozen_at"]):
            raise ValueError("invalid independent reviewer")
        if (
            sub["reviewer"]["kind"] == "fixture_human"
            and pack["provenance"] != "synthetic_fixture"
        ):
            raise ValueError("fixture reviewers cannot label a real corpus")
        for ident, label in sub["labels"].items():
            if ident not in by_case:
                raise ValueError("foreign review case")
            validate_label(
                next(c for c in pack["cases"] if c["case_id"] == ident), label
            )
            by_case[ident].append((sub, label))
    if not isinstance(resolutions, dict) or not set(resolutions) <= set(by_case):
        raise ValueError("unknown adjudication case")
    labels, unresolved, audit = {}, [], {}
    for ident, reviews in by_case.items():
        ids = [s["reviewer"]["id"] for s, _ in reviews]
        if len(ids) != len(set(ids)):
            raise ValueError("reviewer cannot label the same case twice")
        if len(reviews) != 2:
            unresolved.append(ident)
            continue
        refs = sorted(s["submission_sha256"] for s, _ in reviews)

        # Rationale wording can differ without a substantive disagreement.
        def substantive(label):
            return {k: v for k, v in label.items() if k != "rationale"}

        disagree = substantive(reviews[0][1]) != substantive(reviews[1][1])
        if disagree:
            resolution = resolutions.get(ident)
            if resolution is None:
                unresolved.append(ident)
                continue
            exact(resolution, {"reviewer", "input_submissions", "label", "resolved_at"})
            who = resolution["reviewer"]
            exact(who, {"id", "kind", "independent_ref"})
            if (
                who["id"] in ids
                or who["kind"] not in {"human", "fixture_human"}
                or not who["independent_ref"]
                or sorted(resolution["input_submissions"]) != refs
            ):
                raise ValueError(
                    "independent third reviewer and exact input hashes required"
                )
            if instant(resolution["resolved_at"]) < max(
                instant(s["submitted_at"]) for s, _ in reviews
            ):
                raise ValueError("adjudication precedes independent reviews")
            case = next(c for c in pack["cases"] if c["case_id"] == ident)
            validate_label(case, resolution["label"])
            label = resolution["label"]
        else:
            if ident in resolutions:
                raise ValueError("cannot replace agreed labels")
            label = reviews[0][1]
        labels[ident] = copy.deepcopy(label)
        audit[ident] = {
            "reviewers": ids,
            "submissions": refs,
            "disagreement": disagree,
            "resolution_sha256": digest(resolutions[ident]) if disagree else None,
        }
    value = {
        "schema_version": "rr06_adjudication_v1",
        "pack_sha256": pack["pack_sha256"],
        "labels": labels,
        "unresolved": unresolved,
        "audit": audit,
        "independence_authenticated": False,
    }
    return {**value, "adjudication_sha256": digest(value)}
