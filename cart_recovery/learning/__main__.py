"""python -m cart_recovery.learning --help"""

import argparse
import json
import os
from pathlib import Path

from .data import canonical
from .experiment import analyze
from .registry import Registry
from .training import train


def private_write(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(canonical(value) + b"\n")


def main():
    parser = argparse.ArgumentParser(
        description="Offline learning jobs. No sends or publication."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    training = commands.add_parser("train")
    training.add_argument("datasets", nargs="+")
    training.add_argument("--train-through", required=True)
    training.add_argument("--calibration-through", required=True)
    training.add_argument("--registry", required=True)
    training.add_argument("--output", required=True)
    experiment = commands.add_parser("experiment")
    experiment.add_argument("datasets", nargs="+")
    experiment.add_argument("--output", required=True)
    promotion = commands.add_parser("promote")
    promotion.add_argument("--registry", required=True)
    promotion.add_argument("--model-id", required=True)
    promotion.add_argument("--reviewer", required=True)
    promotion.add_argument("--review-ref", required=True)
    estimate = commands.add_parser("estimate")
    estimate.add_argument("--registry", required=True)
    estimate.add_argument("--model-id", required=True)
    estimate.add_argument("--features", required=True)
    estimate.add_argument("--as-of", required=True)
    estimate.add_argument("--output", required=True)
    deletion = commands.add_parser("delete-merchant")
    deletion.add_argument("--registry", required=True)
    deletion.add_argument("--merchant-id", required=True)
    args = parser.parse_args()
    if args.command == "train":
        result = train(args.datasets, args.train_through, args.calibration_through)
        Registry(args.registry).register(result)
        private_write(args.output, result)
    elif args.command == "experiment":
        private_write(args.output, analyze(args.datasets))
    elif args.command == "promote":
        Registry(args.registry).promote(args.model_id, args.reviewer, args.review_ref)
    elif args.command == "estimate":
        private_write(
            args.output,
            Registry(args.registry).estimate(
                args.model_id, json.loads(Path(args.features).read_bytes()), args.as_of
            ),
        )
    else:
        print(
            json.dumps(
                {
                    "models_deleted": Registry(args.registry).delete_merchant(
                        args.merchant_id
                    )
                }
            )
        )


if __name__ == "__main__":
    main()
