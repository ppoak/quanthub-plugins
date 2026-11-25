from __future__ import annotations

from .selection import read_selection_rows, selection_query
from .shared import (
    EVALUATION_COLUMNS,
    EVALUATION_FILES,
    EVALUATION_VARIANTS,
    Any,
    Path,
    _ColumnState,
    _sql_list,
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
                direction=self.cfg.direction,
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
        if state.incremental:
            literal = self.storage.sql_literal(existing.as_posix())
            selected = ", ".join(EVALUATION_COLUMNS[metric])
            existing_cutoff_literal = self.storage.sql_literal(state.cutoffs[metric])
            parts.append(
                f"""
                SELECT {selected}
                FROM read_csv_auto({literal}, HEADER=TRUE)
                WHERE CAST(date AS DATE) < CAST({existing_cutoff_literal} AS DATE)
                """
            )
        parts.append(new_query)
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

    def _stage_column(self, state: _ColumnState) -> Path:
        staging = stage_column_directory(state.output_dir)
        try:
            factor = {
                **({"name": self.cfg.factor_name} if self.cfg.factor_name else {}),
                "column": state.factor_column,
            }
            parameters = self._semantic_parameters()
            previous_files = state.config.get("files", {})
            compatible = (state.config.get("factor") == factor
                          and state.config.get("parameters") == parameters
                          and state.config.get("input") == self.input)
            selected = {"returns" if task == "portfolio" else task for task in self.tasks}
            if state.output_dir.exists():
                for path in state.output_dir.iterdir():
                    if path.is_file() and path.name not in {
                        *EVALUATION_FILES.values(), "config.json", "overview.png",
                    }:
                        shutil.copy2(path, staging / path.name)
            for metric, filename in EVALUATION_FILES.items():
                previous = state.output_dir / filename
                if metric in selected:
                    if state.incremental and state.cutoffs[metric] > self.mature_end:
                        shutil.copy2(previous, staging / filename)
                    else:
                        self._copy_query_to_csv(self._metric_query(state, metric), staging / filename)
                elif compatible and filename in previous_files and (
                    state.config.get("partial_end") == self.end
                    or ("partial_end" not in state.config
                        and (self._last_date(previous) == self.mature_end
                             or (metric == "correlation" and not self.reference_factors)))
                ):
                    shutil.copy2(previous, staging / filename)
            files = evaluation_files(staging)
            for metric, filename in EVALUATION_FILES.items():
                if metric not in selected and filename in files and files[filename] != previous_files[filename]:
                    raise ValueError(f"existing metric changed: {state.output_dir / filename}")
            write_config(staging / "config.json", {
                "factor": factor,
                "parameters": parameters,
                "files": files,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                **({"input": self.input} if self.input is not None else {}),
                **({"partial_end": self.end} if len(files) != len(EVALUATION_FILES) else {}),
            })
            return staging
        except Exception:
            shutil.rmtree(staging)
            raise
