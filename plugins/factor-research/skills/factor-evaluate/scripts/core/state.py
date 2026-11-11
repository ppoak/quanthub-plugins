from __future__ import annotations

from .shared import (
    EVALUATION_VARIANTS,
    Any,
    Mapping,
    Path,
    _chunks,
    _ColumnState,
    _parse_memory_bytes,
    dataset_manifest,
    duckdb,
    evaluation_files_match,
    manifests_match,
    read_config,
)


class _EvaluationStateMixin:
    def _run_reference_refresh(self) -> None:
        """Refresh correlation metrics without rebuilding unchanged returns."""

        states = tuple(state for state in self._states if state.reference_refresh)
        if not states:
            return
        full_begin = max(self.begin, self.factor_range[0])
        full_end = self.end
        full_dates = tuple(
            signal_date
            for signal_date in self.signal_dates
            if full_begin <= signal_date <= full_end
        )
        if not full_dates:
            return
        previous_tasks = self.tasks
        self.tasks = ("correlation",)
        self._reference_refresh_active = True
        previous_batch_size = self._reference_correlation_batch_size
        self._reference_correlation_batch_size = max(
            self.settings.reference_correlation_batch_size,
            min(8, len(self.reference_factors)),
        )
        try:
            for state in states:
                factor_literal = self.storage.sql_literal(state.factor_column)
                cutoff_literal = self.storage.sql_literal(full_begin)
                self.storage.execute(
                    "DELETE FROM _evaluation_correlation "
                    f"WHERE factor_column = {factor_literal} "
                    f"AND date >= CAST({cutoff_literal} AS DATE)"
                )
                refresh_date_batch_days = min(
                    self.date_batch_days,
                    self.settings.reference_refresh_date_batch_days,
                )
                blocks = tuple(
                    tuple(block)
                    for block in _chunks(full_dates, refresh_date_batch_days)
                )
                column = (state.factor_column,)
                self._active_factor_batch = column
                try:
                    for block in blocks:
                        self._process_factor_date_block(block, column)
                finally:
                    self._drop_temp_relations(
                        self._factor_batch_relation,
                        self._factor_variants_relation,
                    )
                    self._active_factor_batch = ()
        finally:
            self._reference_correlation_batch_size = previous_batch_size
            self._reference_refresh_active = False
            self.tasks = previous_tasks

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

    def _manifest(self, dataset: str, through: str | None) -> dict[str, Any]:
        key = (dataset, through)
        if key not in self._manifest_cache:
            root = self.storage.table_root(dataset)
            self._manifest_cache[key] = dataset_manifest(
                root.parent,
                root.name,
                through=through,
            )
        return self._manifest_cache[key]

    def _manifest_prefix_matches_current(
        self,
        stored: Any,
        dataset: str,
    ) -> bool:
        """Accept an unchanged historical prefix for incremental evaluation."""

        if not isinstance(stored, Mapping):
            return False
        through = stored.get("through")
        return isinstance(through, str) and manifests_match(
            stored,
            self._manifest(dataset, through),
        )

    def _stored_manifest_covers_current(
        self,
        stored: Any,
        dataset: str,
        required_through: str,
    ) -> bool:
        """Accept a validated prefix that covers the requested interval."""

        if not isinstance(stored, Mapping):
            return False
        through = stored.get("through")
        if not isinstance(through, str) or through < required_through:
            return False
        return manifests_match(stored, self._manifest(dataset, through))

    def _semantic_parameters(self) -> dict[str, Any]:
        return {
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

    def _column_states(
        self,
        current_factor_manifest: Mapping[str, Any],
    ) -> tuple[_ColumnState, ...]:
        states: list[_ColumnState] = []
        for column in self.factor_columns:
            destination = self.output_root / column
            config_path = destination / "config.json"
            config = read_config(config_path)
            factor = config.get("factor", {})
            if factor and (factor.get("name") != self.cfg.factor_name
                    or factor.get("column") != column
                    or factor.get("dataset", self.cfg.factor_name or "_factor_data") != self.factor_source
                    or factor.get("path") != (self.cfg.factor_data.as_posix() if self.cfg.factor_data else None)):
                raise ValueError("existing results identify another input")
            reusable = self._can_reuse_metrics(config, destination)
            incremental = reusable and self._can_increment(config)
            reference_refresh = (
                reusable and not incremental and self._can_reference_refresh(config)
            )
            cutoff = self.begin
            if incremental or reference_refresh:
                last_date = self._last_date(destination / "returns.csv")
                next_date = self._next_signal_date(last_date)
                cutoff = next_date if next_date is not None else "9999-12-31"
                selection_probe = _ColumnState(
                    factor_column=column,
                    output_dir=destination,
                    config=config,
                    cutoff=cutoff,
                    incremental=True,
                    reference_refresh=reference_refresh,
                )
                if len(self._stored_selection_rows(selection_probe)) != len(
                    EVALUATION_VARIANTS
                ):
                    incremental = False
                    reference_refresh = False
                    cutoff = self.begin
            states.append(
                _ColumnState(
                    factor_column=column,
                    output_dir=destination,
                    config=config,
                    cutoff=cutoff,
                    incremental=incremental or reference_refresh,
                    reference_refresh=reference_refresh,
                )
            )
        return tuple(states)

    def _can_reuse_metrics(self, config: Mapping[str, Any], destination: Path) -> bool:
        """Check shared prerequisites for tail and reference-only refreshes."""
        evaluation = config.get("evaluation")
        if set(self.tasks) != {"ic", "portfolio", "correlation"} or self.cfg.rebuild or not isinstance(evaluation, Mapping):
            return False
        stored_end = evaluation.get("requested_end")
        return (
            evaluation.get("status") == "complete"
            and evaluation.get("begin") == self.begin
            and evaluation.get("parameters") == self._semantic_parameters()
            and isinstance(stored_end, str)
            and self.requested_end >= stored_end
            and isinstance(evaluation.get("source_manifests"), Mapping)
            and evaluation_files_match(destination, evaluation)
        )

    def _can_increment(self, config: Mapping[str, Any]) -> bool:
        """Reuse metric prefixes after checking each source snapshot."""
        manifests = config["evaluation"]["source_manifests"]
        if not self._manifest_prefix_matches_current(
            manifests.get("factor"),
            self.factor_source,
        ):
            return False
        if not self._manifest_prefix_matches_current(
            manifests.get("quotes_day"),
            "quotes_day",
        ):
            return False
        if not self._manifest_prefix_matches_current(
            manifests.get("industry_mapping"),
            "industry_mapping",
        ):
            return False
        references = manifests.get("references", {})
        if not isinstance(references, Mapping):
            return False
        for table, stored in references.items():
            if not isinstance(table, str) or not self._manifest_prefix_matches_current(
                stored, table
            ):
                return False
        return True

    def _can_reference_refresh(self, config: Mapping[str, Any]) -> bool:
        """Reuse IC and returns when only reference-factor snapshots changed."""
        evaluation = config["evaluation"]
        if evaluation.get("status") != "complete":
            return False
        manifests = evaluation["source_manifests"]
        for dataset in (self.factor_source, "quotes_day", "industry_mapping"):
            if not self._manifest_prefix_matches_current(
                manifests.get(dataset if dataset != self.factor_source else "factor"),
                dataset,
            ):
                return False
        references = manifests.get("references")
        if not isinstance(references, Mapping) or not references:
            return False
        changed = False
        for table, stored in references.items():
            if not isinstance(table, str):
                return False
            if not self._manifest_prefix_matches_current(stored, table):
                changed = True
        return changed

    def _last_date(self, path: Path) -> str | None:
        if not path.is_file():
            return None
        try:
            row = self.storage.execute(
                "SELECT MAX(TRY_CAST(date AS DATE)) FROM read_csv_auto(?, header=TRUE)",
                [path.as_posix()],
            ).fetchone()
            return row[0].isoformat() if row and row[0] is not None else None
        except duckdb.Error:
            return None

    def _next_signal_date(self, value: str | None) -> str | None:
        if value is None:
            return self.begin
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
                cutoff DATE NOT NULL,
                incremental BOOLEAN NOT NULL
            )
            """
        )
        self.storage.executemany(
            "INSERT INTO _evaluation_cutoffs VALUES (?, ?, ?)",
            [
                (state.factor_column, state.cutoff, state.incremental)
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
