from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FACTOR_PATH_SPLITTER = re.compile(r"::|->|[./:|,;#@\s]+")


@dataclass(frozen=True)
class FactorPath:
    """Reference one factor by its parquet table and factor column."""

    table: str
    column: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "table", validate_identifier(self.table, "factor_table"))
        object.__setattr__(self, "column", validate_identifier(self.column, "factor_column"))

    @classmethod
    def parse(cls, value: str | Sequence[str] | FactorPath) -> FactorPath:
        if isinstance(value, FactorPath):
            return value
        if isinstance(value, tuple) and len(value) == 2:
            table, column = value
            return cls(
                validate_identifier(str(table), "factor_table"),
                validate_identifier(str(column), "factor_column"),
            )
        if not isinstance(value, str):
            raise TypeError("factor path must be a string, a (table, column) tuple, or FactorPath")

        tokens = [token for token in _FACTOR_PATH_SPLITTER.split(value.strip()) if token]
        tokens = [token for token in tokens if token != "data"]
        identifiers = [token for token in tokens if _IDENTIFIER.fullmatch(token)]
        if len(identifiers) < 2:
            raise ValueError(f"factor path must identify both table and column: {value!r}")
        if len(identifiers) > 2 and identifiers[-2:] != tokens[-2:]:
            raise ValueError(f"ambiguous factor path: {value!r}")
        table, column = identifiers[-2], identifiers[-1]
        return cls(
            validate_identifier(table, "factor_table"),
            validate_identifier(column, "factor_column"),
        )

    @property
    def label(self) -> str:
        return f"{self.table}.{self.column}"


def parse_factor_paths(factor_paths: Any) -> tuple[FactorPath, ...]:
    if isinstance(factor_paths, (str, FactorPath)):
        values: Iterable[Any] = (factor_paths,)
    else:
        values = factor_paths
    parsed = tuple(FactorPath.parse(value) for value in values)
    if not parsed:
        raise ValueError("factor_paths is empty")
    return parsed


def is_factor_path_input(value: Any) -> bool:
    if isinstance(value, (str, FactorPath)):
        return True
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        return isinstance(first, (str, FactorPath)) or (
            isinstance(first, tuple)
            and len(first) == 2
            and all(isinstance(part, str) for part in first)
        )
    return False


def validate_identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value
