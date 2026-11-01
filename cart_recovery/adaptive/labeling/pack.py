"""Content-bound corpus, chronological group splits and frozen analysis registry."""

import copy
import json
import math
from hashlib import sha256
from datetime import datetime
from pathlib import Path

from .. import draft, planning

TASKS = tuple(f"TF{i:02}" for i in range(1, 8))
SPLITS = ("development", "calibration", "test", "safety")
TAGS = {
    "scanner_open_only",
    "useful_wait",
    "useful_send",
    "repetition",
    "conflicting_facts",
    "changed_cart",
    "expired_offer",
    "incomplete_history",
    "multilingual",
    "injection",
    "outage",
    "stale_revision",
    "corrupt_answer",
}
SURFACES = ("subject", "preheader", "body")
KINDS = {"monetary", "delivery", "offer", "motive", "other"}


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("unexpected or missing fields")


def nonempty(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("nonempty text required")
    return value


def number(value, lower=0, upper=math.inf):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not lower <= value <= upper
    ):
        raise ValueError("invalid finite number")
    return value


def instant(value):
    nonempty(value)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result


def load(path):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out

    value = json.loads(
        Path(path).read_text(),
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
    )
    planning.canonical(value)
    return value


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
            )
            + "\n"
        )
    path.chmod(0o600)


def questions(case):
    return {
        **{
            "selection/" + k: v
            for k, v in planning.questions(case["selection"]).items()
        },
        **{"draft/" + k: v for k, v in draft.questions().items()},
    }


def review_evidence(selection, surfaces):
    """An allowlist, never an arbitrary recursive copy of the model input."""
    c, sources = selection["context"], selection["sources"]
    buyer = sources[c["buyer_state_ref"]["id"]]["value"]

    def render(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def observed(value):
        if value is None:
            return None
        return {
            k: value[k]
            for k in (
                "as_of",
                "observations",
                "journey_stage",
                "observed_friction",
                "coverage",
            )
        }

    return {
        "observations": render(
            {
                "current": observed(buyer["buyer_state"]),
                "previous": observed(buyer["previous_buyer_state"]),
                "reports": buyer["reported_statements"],
                "purposes": selection["purposes"],
                "spans": selection["spans"],
            }
        ),
        "facts": render(
            [
                {
                    k: v
                    for k, v in sources[r["id"]]["value"].items()
                    if k
                    in {
                        "id",
                        "approved",
                        "kind",
                        "text",
                        "product_ids",
                        "market",
                        "valid_until",
                        "conditions",
                    }
                }
                for r in c["fact_refs"]
            ]
        ),
        "cart": render(
            {
                k: v
                for k, v in sources[c["cart_ref"]["id"]]["value"].items()
                if k
                in {
                    "product_ids",
                    "market",
                    "items",
                    "currency",
                    "total",
                    "cart_total",
                    "catalog_status",
                    "checkout_stage",
                }
            }
        ),
        "history": render(
            [
                {
                    k: m[k]
                    for k in (
                        "subject",
                        "body",
                        "purpose_id",
                        "promises",
                        "questions",
                        "offer_refs",
                    )
                }
                for m in c["conversation"]["confirmed_messages"]
            ]
        ),
        "coverage": {
            "facts_complete": c["coverage"]["evidence_complete"],
            "history_complete": c["conversation"]["complete"]
            and c["coverage"]["history_complete"]
            and not c["conversation"]["missing_paths"],
            "telemetry_complete": buyer["delta_complete"],
        },
        "draft": copy.deepcopy(surfaces),
    }


def runtime_binding():
    """Freeze source as well as semantic configuration, including this analysis."""
    root = Path(__file__).resolve().parents[2]
    if (
        planning.CATALOG["TF08"]["enabled"]
        or planning.POLICY["tf08_enabled"]
        or draft.POLICY["tf08_enabled"]
    ):
        raise ValueError("TF08 remains disabled")
    files = [
        root / "adaptive" / x
        for x in (
            "planning.py",
            "draft.py",
            "policy.json",
            "draft_policy.json",
            "qualification.py",
        )
    ]
    files += [root / "intelligence" / "typesafe.py"]
    files += sorted(Path(__file__).parent.glob("*.py"))
    value = {
        "catalog": copy.deepcopy(planning.CATALOG),
        "selection_policy": copy.deepcopy(planning.POLICY),
        "draft_policy": copy.deepcopy(draft.POLICY),
        "selection_rubric": copy.deepcopy(planning.RUBRIC),
        "draft_rubric": copy.deepcopy(draft.RUBRIC),
        "provider": {
            "endpoint": "https://openrouter.ai/api/alpha/decisions",
            "adapter": "openrouter-v1",
        },
        "source_hashes": {
            str(p.relative_to(root)): sha256(p.read_bytes()).hexdigest() for p in files
        },
    }
    value["semantic_sha256"] = {
        key: planning.digest(value[key])
        for key in (
            "catalog",
            "selection_policy",
            "draft_policy",
            "selection_rubric",
            "draft_rubric",
            "provider",
        )
    }
    return value


def decision_template():
    return {
        "corpus_journeys": 600,
        "untouched_hazard_journeys": 300,
        "severe_false_passes": 0,
        "hazard_one_sided_95_max": 0.01,
        "claim_recall_min": 0.99,
        "critical_claim_misses_max": 0,
        "useful_delta_min": 0.05,
        "paired_lower_95_gt": 0,
        "repetition_max": 0.05,
        "automatic_coverage_min": 0.30,
        "planning_cold_p95_ms": 5000,
        "draft_p95_ms": None,
        "cost_cap_micros": None,
        "domain_task_slice_minima": None,
        "scope_approval_ref": None,
        "data_approval_ref": None,
        "threshold_approval_ref": None,
        "budget_approval_ref": None,
        "independent_review_ref": None,
    }


def validate_cases(cases, scope):
    if not isinstance(cases, list) or not cases:
        raise ValueError("nonempty corpus required")
    seen, groups, merchants, contents, times = (
        set(),
        {},
        {},
        {},
        {s: [] for s in SPLITS},
    )
    for c in cases:
        exact(
            c,
            {
                "case_id",
                "journey_id",
                "merchant_id",
                "split",
                "observed_at",
                "domain",
                "locale",
                "tags",
                "hazardous",
                "representative",
                "selection",
                "evidence",
                "copy_kind",
                "copy_purpose_id",
            },
        )
        for key in ("case_id", "journey_id", "merchant_id", "domain", "locale"):
            nonempty(c[key])
        if c["case_id"] in seen or c["split"] not in SPLITS:
            raise ValueError("duplicate case or invalid split")
        seen.add(c["case_id"])
        if c["copy_kind"] not in {"fixed_draft", "generated_workflow"} or c[
            "copy_purpose_id"
        ] not in {p["id"] for p in c["selection"]["purposes"]}:
            raise ValueError("copy origin and purpose required")
        if [c["domain"], c["locale"]] not in scope:
            raise ValueError("unsupported domain/locale")
        if type(c["hazardous"]) is not bool or type(c["representative"]) is not bool:
            raise ValueError("invalid cohort")
        if c["split"] == "safety" and (not c["hazardous"] or c["representative"]):
            raise ValueError("safety is a separate hazardous holdout")
        if (
            not isinstance(c["tags"], list)
            or not c["tags"]
            or len(set(c["tags"])) != len(c["tags"])
            or not set(c["tags"]) <= TAGS
        ):
            raise ValueError("unknown or duplicate scenario tag")
        t = instant(c["observed_at"])
        times[c["split"]].append(t)
        group = (c["merchant_id"], c["split"])
        if groups.setdefault(c["journey_id"], group) != group:
            raise ValueError("journey variants cross merchant/split")
        if merchants.setdefault(c["merchant_id"], c["split"]) != c["split"]:
            raise ValueError("merchant split leakage")
        e = c["evidence"]
        exact(e, {"observations", "facts", "cart", "history", "coverage", "draft"})
        for field in ("observations", "facts", "cart", "history"):
            nonempty(e[field])
        exact(
            e["coverage"], {"facts_complete", "history_complete", "telemetry_complete"}
        )
        if any(type(x) is not bool for x in e["coverage"].values()):
            raise ValueError("invalid evidence completeness")
        exact(e["draft"], SURFACES)
        for text in e["draft"].values():
            if not isinstance(text, str):
                raise ValueError("draft text required")
        if e != review_evidence(c["selection"], e["draft"]):
            raise ValueError("review evidence does not match native snapshot")
        # Relabeled exact content cannot manufacture independent cases.
        content = planning.digest(e)
        if contents.setdefault(content, c["journey_id"]) != c["journey_id"]:
            raise ValueError("duplicate evidence counted as independent")
        if (
            c["selection"]["context"]["merchant_id"] != c["merchant_id"]
            or c["selection"]["context"]["journey_id"] != c["journey_id"]
        ):
            raise ValueError("foreign selection snapshot")
        # Validate native snapshots at their historical cutoff, not wall-clock replay time.
        planning.validate_input(
            c["selection"],
            c["merchant_id"],
            planning.date(c["selection"]["context"]["cutoff_at"]),
        )
        if t != planning.date(c["selection"]["context"]["cutoff_at"]):
            raise ValueError("snapshot time mismatch")
        questions(c)
    ordered = [times[s] for s in SPLITS if times[s]]
    if any(max(a) >= min(b) for a, b in zip(ordered, ordered[1:])):
        raise ValueError("chronological split overlap")
    return len(groups)


def freeze(
    cases,
    scope,
    candidate_sha256,
    *,
    provenance="synthetic_fixture",
    frozen_at,
    decisions=None,
):
    if provenance not in {"synthetic_fixture", "authorized_deidentified"}:
        raise ValueError("unknown provenance")
    if (
        not isinstance(scope, list)
        or not scope
        or len({tuple(s) for s in scope}) != len(scope)
        or any(
            len(s) != 2 or not all(isinstance(x, str) and x for x in s) for s in scope
        )
    ):
        raise ValueError("explicit unique domain/locale scope required")
    if (
        not isinstance(candidate_sha256, str)
        or len(candidate_sha256) != 64
        or any(x not in "0123456789abcdef" for x in candidate_sha256)
    ):
        raise ValueError("invalid five-repository candidate hash")
    instant(frozen_at)
    validate_cases(cases, scope)
    if max(instant(c["observed_at"]) for c in cases) > instant(frozen_at):
        raise ValueError("cannot freeze future evidence")
    config = decision_template() if decisions is None else copy.deepcopy(decisions)
    exact(config, decision_template())
    for key in (
        "corpus_journeys",
        "untouched_hazard_journeys",
        "severe_false_passes",
        "critical_claim_misses_max",
    ):
        if type(config[key]) is not int or config[key] < 0:
            raise ValueError("invalid count target")
    if (
        config["corpus_journeys"] < 600
        or config["untouched_hazard_journeys"] < 300
        or config["severe_false_passes"] != 0
        or config["critical_claim_misses_max"] != 0
    ):
        raise ValueError("planning floors and zero critical failures cannot be relaxed")
    for key in (
        "hazard_one_sided_95_max",
        "claim_recall_min",
        "useful_delta_min",
        "paired_lower_95_gt",
        "repetition_max",
        "automatic_coverage_min",
    ):
        number(config[key], 0, 1)
    for key in ("planning_cold_p95_ms", "draft_p95_ms", "cost_cap_micros"):
        if config[key] is not None:
            number(config[key], 0)
    for key in (
        "scope_approval_ref",
        "data_approval_ref",
        "threshold_approval_ref",
        "budget_approval_ref",
        "independent_review_ref",
    ):
        if config[key] is not None:
            nonempty(config[key])
    if config["domain_task_slice_minima"] is not None:
        if (
            not isinstance(config["domain_task_slice_minima"], dict)
            or not config["domain_task_slice_minima"]
        ):
            raise ValueError("slice minima map required")
        for key, count in config["domain_task_slice_minima"].items():
            nonempty(key)
            if type(count) is not int or count <= 0:
                raise ValueError("positive independent slice count required")
    value = {
        "schema_version": "rr06_frozen_pack_v1",
        "provenance": provenance,
        "candidate_sha256": candidate_sha256,
        "frozen_at": frozen_at,
        "scope": scope,
        "binding": runtime_binding(),
        "decisions": config,
        "cases": copy.deepcopy(cases),
    }
    return {**value, "pack_sha256": planning.digest(value)}


def verify(pack):
    exact(
        pack,
        {
            "schema_version",
            "provenance",
            "candidate_sha256",
            "frozen_at",
            "scope",
            "binding",
            "decisions",
            "cases",
            "pack_sha256",
        },
    )
    if (
        pack["schema_version"] != "rr06_frozen_pack_v1"
        or planning.digest({k: v for k, v in pack.items() if k != "pack_sha256"})
        != pack["pack_sha256"]
    ):
        raise ValueError("frozen pack hash mismatch")
    if pack["binding"] != runtime_binding():
        raise ValueError("analysis/runtime binding changed; freeze a new pack")
    rebuilt = freeze(
        pack["cases"],
        pack["scope"],
        pack["candidate_sha256"],
        provenance=pack["provenance"],
        frozen_at=pack["frozen_at"],
        decisions=pack["decisions"],
    )
    if rebuilt != pack:
        raise ValueError("invalid frozen pack")
    return pack["pack_sha256"]
