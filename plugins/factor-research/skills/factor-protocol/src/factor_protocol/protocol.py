from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .storage import FactorStorage


class FactorProtocolError(ValueError):
    """Raised when a factor module does not satisfy the required protocol."""


@dataclass(frozen=True)
class FactorPackage:
    factor_name: str
    factor_root: Path
    module_path: Path
    # Native validators write here only while the public validation dispatcher
    # normalizes their output under ARTIFACTS/factor_zoo/<factor>/<column>.
    dist_dir: Path
    fig_dir: Path

    @property
    def output_dir(self) -> Path:
        return self.dist_dir


@dataclass(frozen=True)
class FactorCalculateContext:
    storage: FactorStorage
    factor_name: str
    begin: str
    end: str | None
    data_root: str
    output_dir: Path
    threads: int
    factor_columns: tuple[str, ...] | None = None
    # Number of trading dates produced by one factor calculation batch.
    compute_batch_days: int | None = None


@dataclass(frozen=True)
class FactorValidateContext:
    storage: FactorStorage
    factor_name: str
    begin: str
    end: str
    output_dir: Path
    factor_columns: tuple[str, ...]
    codes: tuple[str, ...] | None = None
    tolerance: float = 1e-10
    relative_tolerance: float = 1e-8


@dataclass(frozen=True)
class FactorValidationDefinition:
    """Factor-owned independent recomputation callbacks and input contract."""

    factor_name: str
    factor_columns: tuple[str, ...]
    validation_input_columns: tuple[str, ...]
    compute_validation_audit: Callable[..., Any]
    compute_validation_audits: Callable[..., Any] | None = None


class CalculateFactor(Protocol):
    def __call__(self, *, context: FactorCalculateContext) -> None: ...


class ValidateFactor(Protocol):
    def __call__(self, *, context: FactorValidateContext) -> Path | None: ...


def define_factor_validator(
    compute_validation_audit: Callable[..., Any],
    *,
    factor_name: str,
    factor_columns: Sequence[str],
    validation_input_columns: Sequence[str],
    compute_validation_audits: Callable[..., Any] | None = None,
) -> FactorValidationDefinition:
    """Declare independent factor validation callbacks without running them."""

    columns = tuple(str(value) for value in factor_columns)
    inputs = tuple(str(value) for value in validation_input_columns)
    if not factor_name or not columns or not inputs:
        raise FactorProtocolError(
            "factor validation requires a name, output columns, and input columns"
        )
    return FactorValidationDefinition(
        factor_name=str(factor_name),
        factor_columns=columns,
        validation_input_columns=inputs,
        compute_validation_audit=compute_validation_audit,
        compute_validation_audits=compute_validation_audits,
    )


def call_with_supported_kwargs(func: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    filtered = _filter_callable_kwargs(func, kwargs)
    missing = _missing_required_kwargs(func, filtered)
    if missing:
        raise TypeError(
            f"{func.__name__} has unsupported required parameters: "
            f"{', '.join(missing)}"
        )
    return func(**filtered)


def callable_accepts(func: Callable[..., Any], name: str) -> bool:
    signature = inspect.signature(func)
    return name in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _filter_callable_kwargs(func: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    signature = inspect.signature(func)
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return {key: value for key, value in kwargs.items() if value is not None}
    return {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters and value is not None
    }


def _missing_required_kwargs(func: Callable[..., Any], kwargs: dict[str, Any]) -> list[str]:
    signature = inspect.signature(func)
    missing: list[str] = []
    for name, parameter in signature.parameters.items():
        if parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if name not in kwargs:
            missing.append(name)
    return missing


def dedupe(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if str(item)))


def resolve_factor_columns(module: Any) -> tuple[str, ...]:
    raw_columns = getattr(module, "FACTOR_COLUMNS", None)
    if raw_columns is None:
        raise FactorProtocolError("factor module must define FACTOR_COLUMNS")
    if isinstance(raw_columns, str):
        raise FactorProtocolError("FACTOR_COLUMNS must be a sequence of column names")
    resolved = tuple(dedupe(raw_columns))
    if not resolved:
        raise FactorProtocolError("FACTOR_COLUMNS must not be empty")
    return resolved
