"""Maintain numbered acceptance items in factor README section 4."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4


def acceptance_items(content: str) -> list[re.Match[str]]:
    section = re.search(r"(?ms)^## 4[.] [^\n]+\n.*?(?=^## |\Z)", content)
    if section is None:
        raise ValueError("factor README has no acceptance section")
    return list(re.compile(r"(?m)^- \[[ xX]\] .+$").finditer(content, section.start(), section.end()))


def set_acceptance(content: str, item: int, passed: bool) -> str:
    matches = acceptance_items(content)
    if item < 1 or item > len(matches):
        raise ValueError(f"factor README has no acceptance item {item}")
    match = matches[item - 1]
    return content[:match.start() + 3] + ("x" if passed else " ") + content[match.start() + 4:]


def update_acceptance(path: Path, item: int, passed: bool) -> None:
    original = path.read_text(encoding="utf-8")
    updated = set_acceptance(original, item, passed)
    if updated == original:
        return
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(updated, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
