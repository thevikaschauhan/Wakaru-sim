"""Preregistered intention-to-treat analysis, separate from model suitability."""

import math
import statistics

from .data import load, moment
from .model import wilson
from decimal import Decimal, localcontext


def analyze(paths):
    rows, datasets, policies = load(paths)
    reports = []
    for (merchant, experiment), policy in sorted(policies.items()):
        units = {}
        for row in sorted(rows, key=lambda r: (r["decided_at"], r["decision_id"])):
            assignment = row["assignment"]
            if (
                row["merchant_id"] != merchant
                or not assignment
                or assignment["experiment_id"] != experiment
                or not row["first_unit_decision"]
            ):
                continue
            if row["unit_sha256"] in units:
                raise ValueError("duplicate_experiment_unit")
            units[row["unit_sha256"]] = row
        census = {}
        scoped = [d for d in datasets if d["merchant_id"] == merchant]
        for dataset in scoped:
            for assignment in dataset["assignment_units"]:
                if assignment["experiment_id"] != experiment:
                    continue
                unit = assignment["unit_sha256"]
                if unit in census and census[unit] != assignment:
                    raise ValueError("conflicting_assignment_census")
                census[unit] = assignment
        arms, labels = {}, {}
        for arm in ("no_contact", "recovery"):
            arm_rows = [r for r in units.values() if r["assignment"]["arm"] == arm]
            known = [
                r["paid_label"]
                for r in arm_rows
                if r["paid_label"] is not None
                and moment(r["mature_at"]) <= moment(r["dataset_as_of"])
            ]
            labels[arm] = known
            assigned = sum(a["arm"] == arm for a in census.values())
            arms[arm] = {
                "assigned": assigned,
                "mature_known": len(known),
                "unknown": assigned - len(known),
                "positive": sum(known),
                "observed_rate": statistics.fmean(known) if known else None,
                "observed_rate_95ci": wilson(sum(known), len(known)),
                "possible_contacts": sum(
                    r["exposure"].get("possible_contact", False)
                    or r["exposure"]["contaminated"]
                    or r["exposure"]["provider_accepted"]
                    for r in arm_rows
                ),
                "delivered_events": sum(r["exposure"]["delivered"] for r in arm_rows),
            }
        baseline = policy["baseline_rate"]
        alternative = baseline + policy["minimum_effect"]
        pooled = (baseline + alternative) / 2
        numerator = 1.96 * math.sqrt(2 * pooled * (1 - pooled)) + 0.841621 * math.sqrt(
            baseline * (1 - baseline) + alternative * (1 - alternative)
        )
        required = math.ceil(numerator**2 / policy["minimum_effect"] ** 2)
        gates = {
            "full_enrollment_coverage": any(
                moment(d["decisions_from"]) <= moment(policy["starts_at"])
                and moment(d["decisions_through"]) >= moment(policy["ends_at"])
                and moment(d["as_of"]) >= moment(policy["analysis_at"])
                for d in scoped
            ),
            "real_execution": bool(units)
            and all(
                r["origin"] == "real" and r["assignment"]["mode"] == "execution"
                for r in units.values()
            ),
            "preregistered_power": policy["min_per_arm"] >= required
            and policy["min_per_arm"] * baseline >= 5
            and policy["min_per_arm"] * (1 - alternative) >= 5
            and moment(policy["registered_at"]) < moment(policy["starts_at"]),
            "analysis_window_closed": bool(units)
            and all(
                moment(r["dataset_as_of"]) >= moment(policy["analysis_at"])
                for r in units.values()
            ),
            "sample": all(
                a["assigned"] >= policy["min_per_arm"] for a in arms.values()
            ),
            "complete_labels": all(a["unknown"] == 0 for a in arms.values()),
        }
        qualified = all(gates.values())
        difference, interval = None, None
        if qualified:
            control, treatment = arms["no_contact"], arms["recovery"]
            difference = treatment["observed_rate"] - control["observed_rate"]
            # Newcombe interval from Wilson components, including zero-event arms.
            c_low, c_high = control["observed_rate_95ci"]
            t_low, t_high = treatment["observed_rate_95ci"]
            pc, pt = control["observed_rate"], treatment["observed_rate"]
            interval = [
                max(-1, difference - math.sqrt((pt - t_low) ** 2 + (c_high - pc) ** 2)),
                min(1, difference + math.sqrt((t_high - pt) ** 2 + (pc - c_low) ** 2)),
            ]
        net = (
            net_value_difference(
                [r for r in units.values() if r["assignment"]["arm"] == "no_contact"],
                [r for r in units.values() if r["assignment"]["arm"] == "recovery"],
            )
            if qualified
            else None
        )
        reports.append(
            {
                "merchant_id": merchant,
                "experiment_id": experiment,
                "policy": policy,
                "arms": arms,
                "gates": gates,
                "status": "qualified_itt" if qualified else "insufficient_evidence",
                "itt_paid_rate_difference": difference,
                "itt_paid_rate_difference_95ci": interval,
                "incremental_net_value": net,
                "net_value_status": "complete"
                if net is not None
                else "costs_incomplete_or_analysis_unqualified",
                "contaminated_holdout_units": arms["no_contact"]["possible_contacts"],
                "interpretation": "ITT retains contaminated units; provider acceptance, delivery and human exposure are distinct. No causal claim from synthetic or incomplete evidence.",
            }
        )
    return {
        "schema_version": "recovery_experiment_analysis_v1",
        "datasets": [
            {k: v for k, v in d.items() if k != "assignment_units"} for d in datasets
        ],
        "experiments": reports,
    }


def net_value_difference(control, treatment):
    """Exact-money primary endpoint for complete, same-currency cost accounting.

    Current provider ledgers do not establish complete costs, so normal exports
    remain unavailable. Never silently replace net value with gross revenue.
    """
    if not control or not treatment:
        return None
    with localcontext() as context:
        context.prec = 80
        groups = []
        currencies = set()
        for arm in (control, treatment):
            values = []
            for row in arm:
                accounting = row["accounting"]
                if (
                    accounting["status"] != "complete"
                    or accounting["net_value"] is None
                    or len(accounting["received_less_refunds"]) != 1
                ):
                    return None
                currencies.update(accounting["received_less_refunds"])
                value = Decimal(accounting["net_value"])
                if not value.is_finite():
                    return None
                values.append(value)
            groups.append(sum(values) / len(values))
        if len(currencies) != 1:
            return None
        return {
            "currency": next(iter(currencies)),
            "mean_difference": str(groups[1] - groups[0]),
        }
