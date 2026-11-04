"""Validate the structural contract of a QuantHub factor README."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from factor_protocol.readme import acceptance_items


REQUIRED_HEADINGS = (
    "## 1. 因子动机",
    "## 2. 定义与数据",
    "## 3. 参数与输出",
    "## 4. 验收标准",
    "## 5. 参考文献",
)
TEST_SECTION_HEADINGS = (
    "## 6. 因子验证",
    "## 7. 性能测试",
    "## 8. 因子评估",
    "## 9. 因子速览",
)
REQUIRED_ACCEPTANCE_TERMS = (
    "calculate",
    "validate",
    "未来数据",
    "唯一键",
    "峰值 RSS",
    "evaluate",
)
README_STATUSES = ("pass", "fail", "timeout", "oom", "pending", "blocked")
ARTIFACT_STATUSES = ("complete", "stale")
VALID_STATUSES = README_STATUSES + ARTIFACT_STATUSES
DATE_PATTERN = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"README not found: {path}"]
    text = path.read_text(encoding="utf-8")
    for heading in REQUIRED_HEADINGS:
        if heading not in text:
            errors.append(f"missing heading: {heading}")
    for heading in TEST_SECTION_HEADINGS:
        if heading not in text:
            errors.append(f"missing heading: {heading}")
    acceptance_start = text.find("## 4. 验收标准")
    result_positions = [text.find(heading) for heading in TEST_SECTION_HEADINGS]
    result_positions = [position for position in result_positions if position >= 0]
    results_start = min(result_positions) if result_positions else -1
    if not result_positions:
        errors.append("missing test-results heading: ## 6. 因子验证 or ## 7. 性能测试")
    if acceptance_start >= 0:
        acceptance = text[acceptance_start:results_start if results_start > acceptance_start else None]
        for term in REQUIRED_ACCEPTANCE_TERMS:
            if term not in acceptance:
                errors.append(f"acceptance section missing: {term}")
    try:
        if len(acceptance_items(text)) != 8:
            errors.append("acceptance section must contain exactly 8 checklist items")
    except ValueError as exc:
        errors.append(str(exc))
    if results_start >= 0:
        results = text[results_start:]
        if "测试日期" not in results:
            errors.append("test-results section must include 测试日期")
        if not (DATE_PATTERN.search(results) or "YYYY-MM-DD" in results):
            errors.append("test-results section must contain at least one YYYY-MM-DD date")
        if not any(status in results for status in VALID_STATUSES):
            errors.append("test-results section must use a recognized status")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("readme", type=Path)
    args = parser.parse_args()
    errors = validate(args.readme)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"PASS: {args.readme}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
