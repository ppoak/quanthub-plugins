from __future__ import annotations

from factor_protocol.artifacts import manifest_reference
from .selection import read_selection_rows, selection_query
from .shared import (
    EVALUATION_COLUMNS,
    EVALUATION_FILES,
    EVALUATION_VARIANTS,
    Any,
    FactorPath,
    Mapping,
    Path,
    _ColumnState,
    _empty_metric_query,
    _sql_list,
    copy_if_present,
    datetime,
    evaluation_files,
    shutil,
    stage_column_directory,
    timezone,
    write_config,
)


class _EvaluationResultsMixin:
    def _stored_selection_rows(self, state: _ColumnState) -> list[tuple[Any, ...]]:
        return read_selection_rows(
            self.storage,
            state.output_dir / "ic.csv",
            state.factor_column,
            self._semantic_parameters(),
            self.begin,
        )

    def _create_selection_results(self) -> None:
        """Select direction on train and the displayed variant on validation."""

        factor_columns = _sql_list(self.storage, self.factor_columns)
        requested = f"""
            SELECT factor_column FROM _evaluation_cutoffs
            WHERE factor_column IN ({factor_columns})
              AND factor_column NOT IN (SELECT factor_column FROM _evaluation_selection)
        """
        self.storage.execute(
            "INSERT INTO _evaluation_selection "
            + selection_query(
                begin=self.begin,
                train_end=self.cfg.train_end,
                validation_end=self.cfg.validation_end,
                ic_source="SELECT * FROM _evaluation_ic",
                requested_source=requested,
            )
        )
        invalid = self.storage.execute(
            f"""
            SELECT cutoffs.factor_column
            FROM _evaluation_cutoffs cutoffs
            LEFT JOIN _evaluation_selection selection
              ON selection.factor_column = cutoffs.factor_column
            WHERE cutoffs.factor_column IN ({factor_columns})
            GROUP BY cutoffs.factor_column
            HAVING COUNT(selection.variant) <> {len(EVALUATION_VARIANTS)}
                OR COUNT(*) FILTER (WHERE selection.selected) <> 1
            ORDER BY cutoffs.factor_column
            """
        ).fetchall()
        if invalid:
            labels = ", ".join(str(row[0]) for row in invalid)
            raise ValueError(
                "training direction or validation variant selection has "
                f"insufficient IC evidence: {labels}"
            )

    def _create_nav_results(self) -> None:
        self._create_selection_results()
        self.storage.execute(
            """
            CREATE TEMP TABLE _evaluation_returns_nav AS
            WITH direction AS (
                SELECT DISTINCT factor_column, direction_sign
                FROM _evaluation_selection
            ),
            selected_top AS (
                SELECT
                    returns.*,
                    CASE
                        WHEN direction.direction_sign > 0
                        THEN returns.top_high_gross_return
                        ELSE returns.top_low_gross_return
                    END AS top_gross_return,
                    CASE
                        WHEN direction.direction_sign > 0
                        THEN returns.top_high_net_return_5bps
                        ELSE returns.top_low_net_return_5bps
                    END AS top_net_return_5bps,
                    CASE
                        WHEN direction.direction_sign > 0
                        THEN returns.top_high_turnover
                        ELSE returns.top_low_turnover
                    END AS top_turnover,
                    CASE
                        WHEN direction.direction_sign > 0
                        THEN returns.top_high_holdings
                        ELSE returns.top_low_holdings
                    END AS top_holdings,
                    CASE
                        WHEN direction.direction_sign > 0
                        THEN returns.top_high_observations
                        ELSE returns.top_low_observations
                    END AS top_observations
                FROM _evaluation_returns returns
                JOIN direction USING (factor_column)
            ),
            seeded AS (
                SELECT
                    returns.*,
                    COALESCE(layer_seed.gross_seed, 1.0) AS layer_gross_seed,
                    COALESCE(layer_seed.net_seed, 1.0) AS layer_net_seed
                FROM selected_top returns
                LEFT JOIN _layer_nav_seed layer_seed
                  USING (factor_column, variant, portfolio_id, layer)
            )
            SELECT
                factor_column,
                date,
                variant,
                portfolio_id,
                layer,
                gross_return,
                net_return_5bps,
                layer_gross_seed
                    * PRODUCT(1.0 + gross_return) OVER (
                        PARTITION BY factor_column, variant, portfolio_id, layer
                        ORDER BY date
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS gross_return_nav,
                layer_net_seed
                    * PRODUCT(1.0 + net_return_5bps) OVER (
                        PARTITION BY factor_column, variant, portfolio_id, layer
                        ORDER BY date
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS net_return_5bps_nav,
                turnover,
                holdings,
                observations,
                top_gross_return,
                top_net_return_5bps,
                top_turnover,
                top_holdings,
                top_observations
            FROM seeded
            """
        )
        # Top returns are repeated across layers.  Compute their NAV once per
        # date/portfolio and join it back instead of compounding every layer.
        self.storage.execute(
            """
            CREATE TEMP TABLE _evaluation_top_nav AS
            WITH daily AS (
                SELECT DISTINCT
                    factor_column,
                    date,
                    variant,
                    portfolio_id,
                    top_gross_return,
                    top_net_return_5bps
                FROM _evaluation_returns_nav
            )
            SELECT
                daily.factor_column,
                daily.date,
                daily.variant,
                daily.portfolio_id,
                COALESCE(seed.gross_seed, 1.0)
                    * PRODUCT(1.0 + daily.top_gross_return) OVER (
                        PARTITION BY daily.factor_column, daily.variant, daily.portfolio_id
                        ORDER BY daily.date
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS top_gross_return_nav,
                COALESCE(seed.net_seed, 1.0)
                    * PRODUCT(1.0 + daily.top_net_return_5bps) OVER (
                        PARTITION BY daily.factor_column, daily.variant, daily.portfolio_id
                        ORDER BY daily.date
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS top_net_return_5bps_nav
            FROM daily
            LEFT JOIN _top_nav_seed seed
              USING (factor_column, variant, portfolio_id)
            """
        )

    def _metric_query(self, state: _ColumnState, metric: str) -> str:
        factor_literal = self.storage.sql_literal(state.factor_column)
        if metric == "ic":
            new_query = f"""
                SELECT date, variant, ic, observations
                FROM _evaluation_ic
                WHERE factor_column = {factor_literal}
            """
            order = "date, variant"
        elif metric == "correlation":
            new_query = f"""
                SELECT date, variant, reference_factor, correlation, observations
                FROM _evaluation_correlation
                WHERE factor_column = {factor_literal}
            """
            order = "date, variant, reference_factor"
        elif metric == "returns":
            new_query = f"""
                SELECT
                    returns.date,
                    returns.variant,
                    returns.portfolio_id,
                    returns.layer,
                    returns.gross_return,
                    returns.net_return_5bps,
                    returns.gross_return_nav,
                    returns.net_return_5bps_nav,
                    returns.turnover,
                    returns.holdings,
                    returns.observations,
                    returns.top_gross_return,
                    returns.top_net_return_5bps,
                    top.top_gross_return_nav,
                    top.top_net_return_5bps_nav,
                    returns.top_turnover,
                    returns.top_holdings,
                    returns.top_observations
                FROM _evaluation_returns_nav returns
                JOIN _evaluation_top_nav top
                  USING (factor_column, date, variant, portfolio_id)
                WHERE returns.factor_column = {factor_literal}
            """
            order = "date, variant, portfolio_id, layer"
        else:
            raise ValueError(f"unknown evaluation metric: {metric}")

        parts: list[str] = []
        existing = state.output_dir / EVALUATION_FILES[metric]
        preserve_existing = state.incremental and existing.is_file()
        if metric == "correlation" and state.reference_refresh:
            preserve_existing = False
        if preserve_existing:
            literal = self.storage.sql_literal(existing.as_posix())
            selected = ", ".join(EVALUATION_COLUMNS[metric])
            existing_cutoff_literal = self.storage.sql_literal(state.cutoff)
            parts.append(
                f"""
                SELECT {selected}
                FROM read_csv_auto({literal}, HEADER=TRUE)
                WHERE CAST(date AS DATE) < CAST({existing_cutoff_literal} AS DATE)
                """
            )
        has_new_rows = state.cutoff <= self.end
        if metric == "correlation" and state.reference_refresh:
            has_new_rows = True
        parts.append(new_query if has_new_rows else _empty_metric_query(metric))
        return (
            "SELECT * FROM ("
            + "\nUNION ALL\n".join(parts)
            + f") combined ORDER BY {order}"
        )

    def _copy_query_to_csv(self, query: str, path: Path) -> None:
        self.storage.execute(
            f"COPY ({query}) TO {self.storage.sql_literal(path.as_posix())} "
            "(FORMAT CSV, HEADER TRUE, DELIMITER ',')"
        )

    def _source_manifests(self) -> dict[str, Any]:
        last_signal_date = self.signal_dates[-1]
        last_signal_index = self._calendar_index[last_signal_date]
        quote_index = min(
            last_signal_index + self.cfg.execution_lag_days + self.cfg.horizon_days,
            len(self.quote_dates) - 1,
        )
        quote_through = self.quote_dates[quote_index]
        references: dict[str, Any] = {}
        for value in self.reference_factors:
            table = FactorPath.parse(value).table
            references[table] = manifest_reference(
                self._manifest(table, last_signal_date)
            )
        return {
            "factor": manifest_reference(
                self._manifest(self.factor_source, last_signal_date)
            ),
            "quotes_day": manifest_reference(
                self._manifest("quotes_day", quote_through)
            ),
            "industry_mapping": manifest_reference(
                self._manifest("industry_mapping", last_signal_date)
            ),
            "references": references,
        }

    def _stage_column(
        self,
        state: _ColumnState,
    ) -> Path:
        staging = stage_column_directory(state.output_dir)
        try:
            if state.output_dir.exists():
                selected = {"returns" if task == "portfolio" else task for task in self.cfg.tasks}
                excluded = {*(EVALUATION_FILES[m] for m in selected), "config.json", "overview.png"}
                for path in state.output_dir.iterdir():
                    if path.is_file() and path.name not in excluded:
                        shutil.copy2(path, staging / path.name)
            selected = {"returns" if task == "portfolio" else task for task in self.cfg.tasks}
            for metric, filename in EVALUATION_FILES.items():
                if metric not in selected:
                    continue
                self._copy_query_to_csv(
                    self._metric_query(state, metric),
                    staging / filename,
                )
            payload = {
                **state.config,
                "factor": {
                    **state.config.get("factor", {}),
                    **({"name": self.cfg.factor_name} if self.cfg.factor_name else {}),
                    "column": state.factor_column,
                    **({"path": self.cfg.factor_data.as_posix()} if self.cfg.factor_data else {}),
                    **(
                        {"dataset": self.factor_source}
                        if self.cfg.factor_name and self.factor_source != self.cfg.factor_name
                        else {}
                    ),
                },
                "evaluation": {
                    "status": "complete",
                    "begin": self.begin,
                    "requested_end": self.requested_end,
                    "parameters": self._semantic_parameters(),
                    "resources": {
                        "threads": self.cfg.threads,
                        "memory_limit": self.cfg.memory_limit,
                        "column_batch_size": self.column_batch_size,
                        "date_batch_days": self.date_batch_days,
                        "intra_column_workers": self.cfg.intra_column_workers,
                    },
                    "files": {name: digest for name, digest in evaluation_files(staging).items()},
                    "source_manifests": self._source_manifests(),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            evaluation = payload["evaluation"]
            records = dict(state.config.get("evaluation", {}).get("tasks", {}))
            # Legacy complete results can be adopted without changing their metrics.
            previous = state.config.get("evaluation", {})
            def signature(task, record):
                parameters = dict(record["parameters"])
                sources = dict(record["source_manifests"])
                if task != "correlation":
                    parameters.pop("reference_factors", None)
                    sources.pop("references", None)
                return {"begin": record["begin"], "requested_end": record["requested_end"],
                        "parameters": parameters, "source_manifests": sources}
            filenames = {"ic": "ic.csv", "portfolio": "returns.csv", "correlation": "correlation.csv"}
            if not records and previous.get("status") == "complete":
                records = {task: {**signature(task, previous),
                    "sha256": previous["files"].get(filename)} for task, filename in filenames.items()}
            for task in self.cfg.tasks:
                records[task] = {**signature(task, evaluation),
                    "sha256": evaluation["files"][filenames[task]]}
            evaluation["tasks"] = records
            complete = all(records.get(task) == {**signature(task, evaluation),
                "sha256": evaluation["files"].get(filename)} for task, filename in filenames.items())
            evaluation["status"] = "complete" if complete else "partial"
            write_config(staging / "config.json", payload)
            return staging
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
