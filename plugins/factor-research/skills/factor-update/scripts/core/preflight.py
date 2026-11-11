"""Inspect existing factor data and detect unsafe incremental writes."""
from __future__ import annotations
import uuid
from typing import Sequence
from factor_protocol import FactorStorage
from .sources import _partition_dates, _partition_mtime_ns

def _ensure_factor_output_writable(storage: FactorStorage) -> None:
    """Fail before expensive SQL when the formal factor root is read-only.

    Windows ACL failures otherwise appear only after a complete staging
    calculation, when the workflow tries to replace ``date=...``.  A private
    empty-directory probe exercises the same factor-root create/delete
    capability without touching a materialized partition.  The probe is used
    only on real update paths; dry-runs remain read-only.
    """

    factor_root = storage.factor_root
    if not factor_root.is_dir():
        return
    probe = factor_root / f".factor_update_write_probe-{uuid.uuid4().hex}"
    created = False
    try:
        probe.mkdir()
        created = True
        probe.rmdir()
        created = False
    except Exception as exc:  # noqa: BLE001 - preserve the ACL/error detail.
        if created:
            try:
                probe.rmdir()
            except OSError:
                pass
        raise PermissionError(
            f"factor output directory is not writable: {factor_root.as_posix()}"
        ) from exc


def _changed_source_dates(
    storage: FactorStorage,
    *,
    source_dates: Sequence[str],
    source_tables: Sequence[str],
    source_factors: Sequence[str] = (),
    lookback: int = 400,
) -> list[str]:
    """Compare recent source partitions with their existing factor outputs."""
    changed: set[str] = set()
    datasets = tuple(dict.fromkeys((*source_tables, *source_factors)))
    candidate_dates = sorted(set(source_dates))[-max(int(lookback), 1) :]
    for value in candidate_dates:
        output_mtime = _partition_mtime_ns(storage.factor_root / f"date={value}")
        if output_mtime and any(
            _partition_mtime_ns(storage.table_root(dataset) / f"date={value}") > output_mtime
            for dataset in datasets
        ):
            changed.add(value)
    return sorted(changed)


def _stored_factor_columns(storage: FactorStorage) -> tuple[str, ...]:
    """Check the first, middle and latest output schemas before updating."""

    partitions = _partition_dates(storage.factor_root)
    if not partitions:
        return ()
    sample_indices = sorted({0, len(partitions) // 2, len(partitions) - 1})
    columns: set[str] = set()
    for index in sample_indices:
        source = storage.partition_scan_sql(
            storage.factor_name,
            begin=partitions[index],
            end=partitions[index],
        )
        rows = storage.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
        columns.update(
            str(row[0]) for row in rows if str(row[0]) not in {"code", "date", "time"}
        )
    return tuple(sorted(columns))
