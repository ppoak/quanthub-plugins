from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def load_env_file(path: str | Path) -> None:
    path = Path(path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def configure_project(argv: list[str] | None = None) -> Path:
    """Load target settings before parsing source options and relative paths."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project-root", default=".")
    args, _ = parser.parse_known_args(argv)
    root = Path(args.project_root).expanduser().resolve()
    load_env_file(root / ".env")
    os.chdir(root)
    return root


def configured_data_root(variable: str) -> str:
    return os.getenv(variable) or os.getenv("DATA") or "data"


def parse_dated_args(parser: argparse.ArgumentParser) -> argparse.Namespace:
    args = parser.parse_args()
    if args.date is None:
        zone = ZoneInfo(os.getenv("DATABASE_MAINTENANCE_TIMEZONE", "Asia/Shanghai"))
        args.date = datetime.now(zone).date().isoformat()
    return args
