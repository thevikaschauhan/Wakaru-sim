"""Strict, minimized point-in-time learning data and reproducible splits."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

TARGET = "paid_order_within_7d_without_vakaru"
OBSERVED = (
    "cart",
    "checkout",
    "event_count",
    "input_error",
    "inventory_error",
    "payment_error",
)
SEMANTIC = (
    "delivery_timing",
    "interruption",
    "out_of_stock_concern",
    "payment_friction",
    "price_sensitivity",
    "shipping_cost",
    "sizing_doubt",
    "trust_uncertainty",
)


def canonical(value):
    """Sorted JSON with encoding/json number formatting, including tiny scores.

    Python's default 1e-07 differs from Go's 1e-7, and 1e-06 differs from
    0.000001. Hashes must survive an unchanged cross-language export.
    """

    def encode(item):
        if item is None:
            return "null"
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, int):
            return str(item)
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("nonfinite")
            if item == 0:
                return "0"
            value = repr(item)
            if 1e-6 <= abs(item) < 1e21:
                fixed = format(Decimal(value), "f")
                return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
            mantissa, exponent = value.lower().split("e")
            mantissa = mantissa.rstrip("0").rstrip(".") if "." in mantissa else mantissa
            power = int(exponent)
            return mantissa + "e" + ("+" if power >= 0 else "-") + str(abs(power))
        if isinstance(item, str):
            return (
                json.dumps(item, ensure_ascii=False)
                .replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("&", "\\u0026")
                .replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029")
            )
        if isinstance(item, list):
            return "[" + ",".join(encode(v) for v in item) + "]"
        if isinstance(item, dict) and all(isinstance(k, str) for k in item):
            return (
                "{"
                + ",".join(encode(k) + ":" + encode(item[k]) for k in sorted(item))
                + "}"
            )
        raise ValueError("unsupported_json_value")

    return encode(value).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def seal(value, field):
    value[field] = ""
    value[field] = digest(value)
    return value


def moment(value):
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("timezone_required")
    return at


def check_hash(value, field):
    copy = {**value, field: ""}
    if value.get(field) != digest(copy):
        raise ValueError(f"invalid_{field}")


def vector(features, semantic=True):
    if (
        features.get("version") != "recovery_features_v1"
        or set(features.get("observed", {})) != set(OBSERVED)
        or set(features.get("semantic", {})) != set(SEMANTIC)
        or type(features.get("semantic_available")) is not bool
    ):
        raise ValueError("feature_contract")
    observed = [features["observed"][k] for k in OBSERVED]
    inferred = [features["semantic"][k] for k in SEMANTIC]
    for name, value in zip(OBSERVED + SEMANTIC, observed + inferred):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= value <= (500 if name == "event_count" else 1)
        ):
            raise ValueError("feature_bounds")
        if name in OBSERVED and (
            value != int(value) or (name != "event_count" and value not in (0, 1))
        ):
            raise ValueError("observed_feature_domain")
    if not features["semantic_available"] and any(inferred):
        raise ValueError("unavailable_semantic_features")
    return observed + (
        [int(features["semantic_available"])] + inferred if semantic else []
    )


def load(paths):
    """Operator exports only. Origin is an attestation, not proof of authenticity."""
    if not 1 <= len(paths) <= 100:
        raise ValueError("dataset_count_bound")
    rows, datasets, policies = [], [], {}
    seen = set()
    for path in paths:
        if Path(path).stat().st_size > 64 << 20:
            raise ValueError("dataset_bound")
        data = json.loads(
            Path(path).read_bytes(),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")),
        )
        check_hash(data, "dataset_sha256")
        if (
            data["schema_version"] != "recovery_learning_v2"
            or data["origin"] not in {"real", "synthetic"}
            or data["horizon_seconds"] != 604800
            or data["grace_seconds"] != 86400
            or len(data["rows"]) > 10000
        ):
            raise ValueError("dataset_contract")
        cutoff = moment(data["as_of"])
        if cutoff > datetime.now(cutoff.tzinfo):
            raise ValueError("future_dataset")
        for policy in data["experiment_policies"]:
            key = (data["merchant_id"], policy["experiment_id"])
            if key in policies and policies[key] != policy:
                raise ValueError("changed_preregistration")
            policies[key] = policy
        census = {}
        for assignment in data["assignment_units"]:
            unit = (assignment["experiment_id"], assignment["unit_sha256"])
            if (
                unit in census
                or assignment["arm"] not in {"no_contact", "recovery"}
                or assignment["probability"] != 0.5
                or assignment["version"] != "recovery_assignment_v1"
            ):
                raise ValueError("invalid_assignment_census")
            census[unit] = assignment
        for row in data["rows"]:
            check_hash(row, "row_sha256")
            if row["features_sha256"] != digest(row["features"]):
                raise ValueError("feature_hash")
            vector(row["features"])
            if (
                row["target"] != TARGET
                or moment(row["decided_at"]) > cutoff
                or (
                    moment(row["mature_at"]) - moment(row["decided_at"])
                ).total_seconds()
                != 691200
            ):
                raise ValueError("row_time_or_target")
            for label in (row["paid_label"], row["without_vakaru_label"]):
                if label is not None and (
                    type(label) is not int or label not in (0, 1)
                ):
                    raise ValueError("label_domain")
            if (
                row["assignment"] is not None
                and census.get(
                    (
                        row["assignment"]["experiment_id"],
                        row["assignment"]["unit_sha256"],
                    )
                )
                != row["assignment"]
            ):
                raise ValueError("assignment_missing_from_census")
            if row["without_vakaru_label"] is not None:
                a = row["assignment"] or {}
                if (
                    moment(row["mature_at"]) > cutoff
                    or not row["first_unit_decision"]
                    or a.get("arm") != "no_contact"
                    or a.get("mode") != "execution"
                    or a.get("probability") != 0.5
                    or a.get("unit_sha256") != row["unit_sha256"]
                    or row["exposure"]["contaminated"]
                    or row["without_vakaru_label"] != row["paid_label"]
                    or row["outcome_status"] not in {"positive", "negative"}
                ):
                    raise ValueError("untreated_label_gate")
                policy = policies.get((data["merchant_id"], a["experiment_id"]))
                if (
                    not policy
                    or policy["policy_sha256"] != a["policy_sha256"]
                    or moment(policy["registered_at"]) >= moment(policy["starts_at"])
                    or not moment(policy["starts_at"])
                    <= moment(a["assigned_at"])
                    < moment(policy["ends_at"])
                ):
                    raise ValueError("assignment_provenance")
            key = (data["merchant_id"], row["decision_id"])
            if key in seen:
                raise ValueError("overlapping_dataset_decision")
            seen.add(key)
            rows.append(
                {
                    **row,
                    "merchant_id": data["merchant_id"],
                    "origin": data["origin"],
                    "dataset_sha256": data["dataset_sha256"],
                    "dataset_as_of": data["as_of"],
                }
            )
        if len(rows) > 100000:
            raise ValueError("training_row_bound")
        datasets.append(
            {
                k: data[k]
                for k in (
                    "merchant_id",
                    "dataset_sha256",
                    "as_of",
                    "origin",
                    "decisions_from",
                    "decisions_through",
                    "assignment_units",
                )
            }
        )
    return rows, datasets, policies


def split(rows, train_through, calibration_through):
    """Merchant-disjoint, chronological, purged by label maturity.

    Hash strata are fixed before outcomes. No shopper appears twice. Training
    labels must be mature before calibration starts, and calibration labels
    before test starts. Excluded rows remain counted in the report.
    """
    first, second = moment(train_through), moment(calibration_through)
    if first >= second:
        raise ValueError("split_order")
    result = {"train": [], "calibration": [], "test": []}
    units = set()
    for row in sorted(rows, key=lambda r: (r["decided_at"], r["decision_id"])):
        if row["without_vakaru_label"] is None:
            continue
        unit = (row["merchant_id"], row["unit_sha256"])
        if unit in units:
            raise ValueError("duplicate_assignment_unit")
        units.add(unit)
        stratum = int(hashlib.sha256(row["merchant_id"].encode()).hexdigest(), 16) % 5
        at, mature = moment(row["decided_at"]), moment(row["mature_at"])
        if stratum < 3 and mature <= first:
            result["train"].append(row)
        elif stratum == 3 and at >= first and mature <= second:
            result["calibration"].append(row)
        elif stratum == 4 and at >= second:
            result["test"].append(row)
    return result
