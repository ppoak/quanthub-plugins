"""Incrementally update one factor package or a configured schema block."""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import sys

sys.dont_write_bytecode = True

from core.update import run


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError("value must be a positive integer")
    return number


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("--target", required=True,
                        help="Factor package name or schema block name, such as active or core.")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--task", action="append", choices=("update", "readme"),
                        help="Repeat to combine tasks. Default: update, without README writeback.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview selected tasks without writing data, reports or README.")
    parser.add_argument("--threads", type=positive_int,
                        help="Select a passing Benchmark thread count; default: selected Benchmark setting.")
    parser.add_argument("--compute-batch-days", type=positive_int,
                        help="Select a passing Benchmark batch; default: fastest passing batch.")
    parser.add_argument("--memory-limit",
                        help="Select a passing Benchmark memory limit; default: selected Benchmark setting.")
    parser.add_argument("--temp-directory")


def main() -> int:
    parser = ArgumentParser(description=__doc__)
    add_arguments(parser)
    args = parser.parse_args()
    try:
        return run(args, project_root=Path(args.project_root).resolve())
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Update failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
