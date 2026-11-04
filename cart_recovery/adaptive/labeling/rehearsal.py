"""Explicit synthetic rehearsal for the portable RR06 qualification harness."""

import argparse
import socket
from pathlib import Path

from . import fixtures, metrics, pack, reviews


def run(candidate_sha256, output):
    def no_network(*_args, **_kwargs):
        raise AssertionError(
            "RR06 synthetic rehearsal must not open a network connection"
        )

    socket.socket.connect = no_network
    socket.create_connection = no_network
    p = pack.freeze(
        fixtures.cases(),
        [["synthetic-commerce", "en"], ["synthetic-commerce", "fr"]],
        candidate_sha256,
        frozen_at=fixtures.FROZEN_AT,
    )
    submissions, adjudication = fixtures.reviews(p)
    records = fixtures.records(p)
    analysis = metrics.analyze(p, adjudication, records)
    assert (
        not analysis["production_qualified"]
        and not analysis["enablement_authorized"]
        and not analysis["model_quality_proven"]
    )
    assert analysis["safety"]["full"]["hazard_false_pass_journeys"] == 0
    assert analysis["safety"]["without_TF06"]["hazard_false_pass_journeys"] > 0
    assert analysis["claim_coverage"]["critical_misses"] > 0
    assert analysis["total_reserved_unknown_micros"] > 0
    for partition in pack.SPLITS:
        packet, _ = reviews.blind(
            p,
            {
                "id": "fixture-reviewer-a",
                "kind": "fixture_human",
                "independent_ref": "fixture-only",
            },
            b"fixture-blinding-key-only-000000000",
            partition,
        )
        pack.write_new(output / ("blind-" + partition + ".json"), packet)
    for name, value in (
        ("frozen-pack", p),
        ("submissions", submissions),
        ("adjudication", adjudication),
        ("predictions", records),
        ("analysis", analysis),
        (
            "decision-packet",
            {
                "schema_version": "rr06_decisions_v1",
                "provenance": "synthetic_fixture",
                "proposed": p["decisions"],
                "approved": False,
                "G10_closed": False,
                "required_next": [
                    "Lawful deidentified dataset and scope approval",
                    "Named independent human reviewers and privacy access",
                    "Task/domain sample-size review and slice minima",
                    "Latency and total cost caps",
                    "Bounded model/provider spend authorization",
                    "Separate protected holdout access and pre-unblinding decision record",
                    "Authenticated external review and release decision",
                ],
            },
        ),
    ):
        pack.write_new(output / (name + ".json"), value)
    return {
        "pack_sha256": p["pack_sha256"],
        "case_n": len(p["cases"]),
        "submissions": len(submissions),
        "human_labels_created": 0,
        "model_calls": 0,
        "provider_calls": 0,
        "G10_closed": False,
        "production_qualified": False,
        "enablement_authorized": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    pack.write_new(output / "rehearsal.json", run(args.candidate_sha256, output))


if __name__ == "__main__":
    main()
