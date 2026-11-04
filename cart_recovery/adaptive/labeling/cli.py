"""File-only workflow. No network calls, provider execution or release operation."""

import argparse
import os
import stat
import sys
from pathlib import Path

from . import pack, reviews, metrics


def private_key(path):
    # Nonblocking open prevents a FIFO from hanging before its type is checked;
    # O_NOFOLLOW and fstat bind validation/read to the same regular inode.
    path = Path(path)
    if path.is_symlink():
        raise ValueError("blinding key requires a private regular file")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("blinding key requires a private regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            return stream.read()
    finally:
        if fd is not None:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("freeze")
    f.add_argument(
        "--spec",
        required=True,
        help="cases, scope, candidate_sha256, provenance, frozen_at, decisions",
    )
    for name in ("blind", "submit", "adjudicate", "analyze"):
        p = sub.add_parser(name)
        p.add_argument("--pack", required=True)
        if name == "blind":
            p.add_argument("--reviewer", required=True)
            p.add_argument("--key-file", required=True)
            p.add_argument("--partition", choices=pack.SPLITS, required=True)
            p.add_argument("--mapping-output", required=True)
        if name == "submit":
            for flag in ("packet", "mapping", "labels", "submitted-at"):
                p.add_argument("--" + flag, required=True)
        if name in {"adjudicate", "analyze"}:
            p.add_argument(
                "--submissions",
                required=True,
                help="JSON array of immutable review submissions",
            )
            p.add_argument("--resolutions", required=True)
        if name == "analyze":
            p.add_argument("--predictions", required=True)
    for p in sub.choices.values():
        p.add_argument(
            "--output",
            required=True,
            help="new private JSON file; existing files are never overwritten",
        )
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            result = pack.freeze(**pack.load(args.spec))
        else:
            frozen = pack.load(args.pack)
            pack.verify(frozen)
            if args.command == "blind":
                result, mapping = reviews.blind(
                    frozen,
                    pack.load(args.reviewer),
                    private_key(args.key_file),
                    args.partition,
                )
                pack.write_new_pair(
                    [(args.mapping_output, mapping), (args.output, result)]
                )
            elif args.command == "submit":
                result = reviews.submit(
                    frozen,
                    pack.load(args.packet),
                    pack.load(args.mapping),
                    pack.load(args.labels),
                    args.submitted_at,
                )
            else:
                result = reviews.adjudicate(
                    frozen, pack.load(args.submissions), pack.load(args.resolutions)
                )
                if args.command == "analyze":
                    result = metrics.analyze(
                        frozen, result, pack.load(args.predictions)
                    )
        if args.command != "blind":
            pack.write_new(args.output, result)
        print(args.command + ": written locally")
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print("blocked: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
