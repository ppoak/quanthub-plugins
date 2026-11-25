from __future__ import annotations

from pathlib import Path

from .check import EVALUATION_FILES, check_evaluation_config, evaluation_config

from .shared import (
    EVALUATION_VARIANTS,
    Any,
    _ColumnState,
    _parse_memory_bytes,
    read_config,
    resolve_output_root,
)


class _EvaluationStateMixin:
    def _validate_quote_schema(self) -> None:
        source = self.storage._table_scan_source("quotes_day")
        columns = {
            row[0]
            for row in self.storage.execute(
                f"DESCRIBE SELECT * FROM {source}"
            ).fetchall()
        }
        required = {
            "date",
            "code",
            self.cfg.price_col,
            "st",
            "suspended",
            "limit_up_post",
            "limit_down_post",
            "total_shares",
        }
        missing = sorted(required - columns)
        if missing:
            raise ValueError(
                "quotes_day is missing required evaluation columns: "
                + ", ".join(missing)
            )

    def _validate_industry_schema(self) -> None:
        source = self.storage._table_scan_source("industry_mapping")
        columns = {
            row[0]
            for row in self.storage.execute(
                f"DESCRIBE SELECT * FROM {source}"
            ).fetchall()
        }
        required = {"date", "code", self.cfg.neutralization_industry_level}
        missing = sorted(required - columns)
        if missing:
            raise ValueError(
                "industry_mapping is missing required evaluation columns: "
                + ", ".join(missing)
            )

    def _semantic_parameters(self) -> dict[str, Any]:
        return {
            "begin": self.begin,
            "horizon_days": self.cfg.horizon_days,
            "execution_lag_days": self.cfg.execution_lag_days,
            "price_col": self.cfg.price_col,
            "quantiles": self.cfg.quantiles,
            "top_percentile": float(self.cfg.top_percentile),
            "rolling_mean_days": self.cfg.rolling_mean_days,
            "industry_level": self.cfg.neutralization_industry_level,
            "direction": self.cfg.direction,
            "train_end": self.cfg.train_end,
            "validation_end": self.cfg.validation_end,
            "reference_factors": list(self.reference_factors),
        }

    def _column_states(self) -> tuple[_ColumnState, ...]:
        states = []
        for column in self.factor_columns:
            destination = self.output_root / column
            config = evaluation_config(read_config(destination / "config.json"))
            factor = config.get("factor")
            if factor and (
                factor.get("name") != self.cfg.factor_name
                or factor.get("column") != column
                or config.get("input") != self.input
            ):
                raise ValueError(f"existing results identify another input: {destination}")
            if (self.cfg.factor_name and set(self.tasks) != {"ic", "portfolio", "correlation"}
                    and set(config.get("files", {})) == set(EVALUATION_FILES.values())
                    and self.output_root == resolve_output_root(
                        "artifacts/factor_zoo", project_root=self.project_root
                    ) / self.cfg.factor_name):
                raise ValueError("single-task evaluation cannot replace a complete package bundle; use --output-dir")
            defaults = {
                metric: ("9999-12-31" if metric == "correlation" and not self.reference_factors
                         else self.begin)
                for metric in EVALUATION_FILES
            }
            state = _ColumnState(column, destination, config, defaults, False)
            if not self.cfg.rebuild and set(self.tasks) == {"ic", "portfolio", "correlation"}:
                try:
                    state = self._resume_column(state)
                except ValueError as exc:
                    if self.cfg.daily_update:
                        raise ValueError(f"incremental evaluation blocked: {exc}") from exc
            states.append(state)
        return tuple(states)

    def _resume_column(self, state: _ColumnState) -> _ColumnState:
        destination = state.output_dir
        config = check_evaluation_config(
            destination, factor_name=self.cfg.factor_name, factor_source=self.cfg.storage_factor_name,
            factor_data=self.cfg.factor_data, project_root=self.project_root,
        )
        if config["parameters"] != self._semantic_parameters():
            raise ValueError(f"evaluation parameters changed: {destination}; rebuild evaluation")
        if len(self._stored_selection_rows(state)) != len(EVALUATION_VARIANTS):
            raise ValueError(f"existing selection cannot be recovered: {destination}")

        cutoffs = {}
        for metric, filename in EVALUATION_FILES.items():
            if metric == "correlation" and not self.reference_factors:
                cutoffs[metric] = "9999-12-31"
                continue
            path = destination / filename
            date = self._last_date(path)
            if date is None:
                raise ValueError(f"existing result has no valid cutoff: {path}")
            if date > self.mature_end:
                raise ValueError(f"existing result date {date} is ahead of the evaluable range: {path}")
            cutoffs[metric] = self._next_signal_date(date) or "9999-12-31"
        return _ColumnState(state.factor_column, destination, config, cutoffs, True)

    def _last_date(self, path: Path) -> str | None:
        row = self.storage.execute(
            "SELECT MAX(CAST(date AS DATE)) FROM read_csv_auto(?, header=TRUE)",
            [path.as_posix()],
        ).fetchone()
        return row[0].isoformat() if row and row[0] is not None else None

    def _next_signal_date(self, value: str) -> str | None:
        for day in self.signal_dates:
            if day > value:
                return day
        return None

    def _create_calendar(self) -> None:
        self.storage.execute(
            """
            CREATE TEMP TABLE _evaluation_calendar (
                date DATE NOT NULL,
                calendar_seq BIGINT NOT NULL
            )
            """
        )
        self.storage.executemany(
            "INSERT INTO _evaluation_calendar VALUES (?, ?)",
            [(date, index + 1) for index, date in enumerate(self.quote_dates)],
        )
        self.storage.execute(
            """
            CREATE TEMP TABLE _evaluation_cutoffs (
                factor_column VARCHAR NOT NULL,
                ic DATE NOT NULL,
                returns DATE NOT NULL,
                correlation DATE NOT NULL
            )
            """
        )
        self.storage.executemany(
            "INSERT INTO _evaluation_cutoffs VALUES (?, ?, ?, ?)",
            [
                (state.factor_column, *(state.cutoffs[metric] for metric in EVALUATION_FILES))
                for state in self._states
            ],
        )

    def _context_begin(self, signal_date: str) -> str:
        """Return the earliest signal date needed for a turnover context."""

        signal_index = self._calendar_index[signal_date]
        context_index = max(
            self._anchor_signal_index,
            # Period-end exit filtering removes horizon + lag signal dates.
            # Include the preceding cycle for every staggered portfolio.
            signal_index
            - (3 * self.cfg.horizon_days + self.cfg.execution_lag_days - 1),
        )
        return self.quote_dates[context_index]

    def _factor_context_begin(self, signal_date: str) -> str:
        """Return context needed by turnover and rolling neutralized values."""

        turnover_index = self._calendar_index[self._context_begin(signal_date)]
        # The previous portfolio needs its own complete rolling window.
        context_index = max(
            self._anchor_signal_index,
            turnover_index - self.cfg.rolling_mean_days + 1,
        )
        return self.quote_dates[context_index]

    def _batch_sizes(self) -> tuple[int, int]:
        explicit_columns = self.cfg.column_batch_size
        explicit_dates = self.cfg.date_batch_days
        if explicit_columns is not None and explicit_dates is not None:
            return (
                min(int(explicit_columns), len(self.factor_columns)),
                min(int(explicit_dates), len(self.signal_dates)),
            )
        sample_dates = self.signal_dates[
            : min(self.settings.probe_date_days, len(self.signal_dates))
        ]
        source = self.storage._table_scan_source(self.factor_source)
        sample_column = self.storage.identifier(self.factor_columns[0])
        row = self.storage.execute(
            f"""
            SELECT COUNT(*)
            FROM {source}
            WHERE CAST(date AS DATE) >= CAST({self.storage.sql_literal(sample_dates[0])} AS DATE)
              AND CAST(date AS DATE) <= CAST({self.storage.sql_literal(sample_dates[-1])} AS DATE)
              AND isfinite(CAST({sample_column} AS DOUBLE))
            """
        ).fetchone()
        sample_rows = int(row[0]) if row else 0
        rows_per_day = max(sample_rows / max(len(sample_dates), 1), 1.0)
        memory_bytes = _parse_memory_bytes(self.cfg.memory_limit)
        target_bytes = (
            int(memory_bytes * 0.60) if memory_bytes is not None else 2_000_000_000
        )
        column_limit = (
            explicit_columns
            if explicit_columns is not None
            else self.settings.default_column_batch_size
        )
        column_batch = min(int(column_limit), len(self.factor_columns))
        # A ranked candidate row exists in several spillable relations while
        # window functions are active.  Use a deliberately conservative
        # multiplier so the automatic batch stays near 60% of the DuckDB cap
        # after transient sort/hash state is included.
        estimated_row_bytes = 1024
        inferred_dates = max(
            1,
            int(
                target_bytes
                / (rows_per_day * estimated_row_bytes * max(column_batch, 1))
            ),
        )
        # The configured full-history block remains bounded by the inferred
        # memory envelope; an explicit CLI override remains authoritative.
        if explicit_dates is not None:
            # An explicit benchmark/production override is authoritative.
            # The automatic policy remains bounded by the inferred memory
            # envelope, while the block retry path will split an oversized
            # explicit block if DuckDB reports OOM.
            date_batch = min(int(explicit_dates), len(self.signal_dates))
        else:
            requested_date_batch = (
                self.settings.single_column_date_batch_days
                if len(self.factor_columns) == 1
                else self.settings.default_date_batch_days
            )
            date_batch = min(
                requested_date_batch,
                self.settings.default_date_batch_days,
                inferred_dates,
                len(self.signal_dates),
            )
        return max(column_batch, 1), max(date_batch, 1)
