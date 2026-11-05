from __future__ import annotations

from .shared import (
    FactorPath,
    Sequence,
    TRANSACTION_COST,
    _STAGE_TABLE_DEFINITIONS,
    _chunks,
    _is_oom,
    _sql_list,
    duckdb,
)

from .ic import _IcMixin
from .portfolio import _PortfolioMixin
from .correlation import _CorrelationMixin


class _EvaluationComputeMixin(_IcMixin, _PortfolioMixin, _CorrelationMixin):

    def _create_stage_tables(self) -> None:
        for table, columns in _STAGE_TABLE_DEFINITIONS.items():
            definition = ",\n                ".join(columns)
            self.storage.execute(
                f"CREATE TEMP TABLE {table} (\n                {definition}\n            )"
            )
        stored_selection_rows = [
            row
            for state in self._states
            if state.incremental
            for row in self._stored_selection_rows(state)
        ]
        if stored_selection_rows:
            self.storage.executemany(
                "INSERT INTO _evaluation_selection VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                stored_selection_rows,
            )
        for state in self._states:
            if not state.incremental or state.cutoff > self.end:
                continue
            path = state.output_dir / "returns.csv"
            literal = self.storage.sql_literal(path.as_posix())
            column_literal = self.storage.sql_literal(state.factor_column)
            cutoff_literal = self.storage.sql_literal(state.cutoff)
            self.storage.execute(
                f"""
                INSERT INTO _layer_nav_seed
                SELECT
                    {column_literal},
                    CAST(variant AS VARCHAR),
                    CAST(portfolio_id AS INTEGER),
                    CAST(layer AS INTEGER),
                    arg_max(CAST(gross_return_nav AS DOUBLE), CAST(date AS DATE)),
                    arg_max(CAST(net_return_5bps_nav AS DOUBLE), CAST(date AS DATE))
                FROM read_csv_auto({literal}, HEADER=TRUE)
                WHERE CAST(date AS DATE) < CAST({cutoff_literal} AS DATE)
                GROUP BY variant, portfolio_id, layer
                """
            )
            self.storage.execute(
                f"""
                INSERT INTO _top_nav_seed
                SELECT
                    {column_literal},
                    CAST(variant AS VARCHAR),
                    CAST(portfolio_id AS INTEGER),
                    arg_max(CAST(top_gross_return_nav AS DOUBLE), CAST(date AS DATE)),
                    arg_max(CAST(top_net_return_5bps_nav AS DOUBLE), CAST(date AS DATE))
                FROM read_csv_auto({literal}, HEADER=TRUE)
                WHERE CAST(date AS DATE) < CAST({cutoff_literal} AS DATE)
                GROUP BY variant, portfolio_id
                """
            )

    def _drop_temp_relations(self, *relations: str) -> None:
        """Drop temporary tables or views without making cleanup fragile."""

        for relation in relations:
            try:
                self.storage.execute(f"DROP VIEW IF EXISTS {relation}")
            except duckdb.Error:
                pass
            try:
                self.storage.execute(f"DROP TABLE IF EXISTS {relation}")
            except duckdb.Error:
                pass

    def _block_requires_cutoff(self, block_begin: str) -> bool:
        """Whether this block still contains rows before a column cutoff."""

        if self._reference_refresh_active:
            return False
        active = set(self._active_factor_batch)
        states = (
            state
            for state in self._states
            if not active or state.factor_column in active
        )
        return any(state.cutoff > block_begin for state in states)

    def _prepare_shared_forward_returns(
        self,
        process_begin: str,
        process_end: str,
    ) -> None:
        """Materialize the eligible forward-return panel for one date block.

        The scoped temporary quote table also carries the market-cap exposure
        used by factor neutralization.  Both relations are dropped after the
        block so a full-history evaluation never retains the complete panel.
        """

        context_begin = self._factor_context_begin(process_begin)
        context_seq = self._calendar_index[context_begin] + 1
        end_seq = self._calendar_index[process_end] + 1
        minimum_quote_seq = context_seq
        maximum_quote_seq = min(
            end_seq + self.cfg.execution_lag_days + self.cfg.horizon_days,
            len(self.quote_dates),
        )
        minimum_quote_date = self.quote_dates[minimum_quote_seq - 1]
        maximum_quote_date = self.quote_dates[maximum_quote_seq - 1]
        quote_source = self.storage.partition_scan_sql(
            "quotes_day",
            begin=minimum_quote_date,
            end=maximum_quote_date,
        )
        price = self.storage.identifier(self.cfg.price_col)
        self._drop_temp_relations(
            "_evaluation_quotes",
            self._forward_returns_relation,
            "_forward_returns_block",
        )
        self.storage.execute(
            f"""
            CREATE TEMP TABLE _evaluation_quotes AS
            SELECT
                CAST(date AS DATE) AS date,
                CAST(code AS VARCHAR) AS code,
                CAST({price} AS DOUBLE) AS price,
                CAST(limit_up_post AS DOUBLE) AS limit_up,
                CAST(limit_down_post AS DOUBLE) AS limit_down,
                CAST(st AS BOOLEAN) AS st,
                CAST(suspended AS BOOLEAN) AS suspended,
                LN(
                    CASE
                        WHEN isfinite(CAST(close AS DOUBLE))
                         AND CAST(close AS DOUBLE) > 0.0
                         AND isfinite(CAST(total_shares AS DOUBLE))
                         AND CAST(total_shares AS DOUBLE) > 0.0
                        THEN CAST(close AS DOUBLE)
                           * CAST(total_shares AS DOUBLE)
                    END
                ) AS log_market_cap
            FROM {quote_source}
            WHERE CAST(date AS DATE)
                  BETWEEN CAST({self.storage.sql_literal(minimum_quote_date)} AS DATE)
                      AND CAST({self.storage.sql_literal(maximum_quote_date)} AS DATE)
            """
        )
        self.storage.execute(
            f"""
            CREATE TEMP TABLE {self._forward_returns_relation} AS
            SELECT
                signal.date,
                entry.code,
                signal.portfolio_id,
                signal.exit_date,
                CAST(exit.price / entry.price - 1.0 AS DOUBLE)
                    AS gross_security_return
            FROM (
                SELECT
                    signal.date,
                    entry.date AS execution_date,
                    exit.date AS exit_date,
                    CAST(
                        1 + MOD(entry.calendar_seq - 1, {self.cfg.horizon_days})
                        AS INTEGER
                    ) AS portfolio_id
                FROM _evaluation_calendar signal
                JOIN _evaluation_calendar entry
                  ON entry.calendar_seq = signal.calendar_seq
                     + {self.cfg.execution_lag_days}
                JOIN _evaluation_calendar exit
                  ON exit.calendar_seq = signal.calendar_seq
                     + {self.cfg.execution_lag_days + self.cfg.horizon_days}
                WHERE signal.calendar_seq BETWEEN {context_seq} AND {end_seq}
            ) signal
            JOIN _evaluation_quotes entry
              ON entry.date = signal.execution_date
            JOIN _evaluation_quotes exit
              ON exit.date = signal.exit_date
             AND exit.code = entry.code
            WHERE entry.price > entry.limit_down
              AND entry.price < entry.limit_up
              AND entry.st IS FALSE
              AND entry.suspended IS FALSE
              AND isfinite(entry.price)
              AND entry.price > 0.0
              AND isfinite(exit.price)
              AND exit.price > 0.0
              AND (
                    (
                        signal.date <= CAST(
                            {self.storage.sql_literal(self.cfg.train_end)} AS DATE
                        )
                        AND signal.exit_date <= CAST(
                            {self.storage.sql_literal(self.cfg.train_end)} AS DATE
                        )
                    )
                    OR (
                        signal.date > CAST(
                            {self.storage.sql_literal(self.cfg.train_end)} AS DATE
                        )
                        AND signal.date <= CAST(
                            {self.storage.sql_literal(self.cfg.validation_end)} AS DATE
                        )
                        AND signal.exit_date <= CAST(
                            {self.storage.sql_literal(self.cfg.validation_end)} AS DATE
                        )
                    )
                    OR (
                        signal.date > CAST(
                            {self.storage.sql_literal(self.cfg.validation_end)} AS DATE
                        )
                        AND signal.date <= CAST(
                            {self.storage.sql_literal(self.end)} AS DATE
                        )
                        AND signal.exit_date <= CAST(
                            {self.storage.sql_literal(self.end)} AS DATE
                        )
                    )
              )
            """
        )

    def _create_factor_batch(
        self,
        columns: Sequence[str],
        begin: str,
        end: str,
    ) -> None:
        """Unpivot one factor-column batch for the active date block."""

        if not columns:
            return
        factor_begin = max(self._factor_context_begin(begin), self.factor_range[0])
        source = self.storage.partition_scan_sql(
            self.factor_source,
            begin=factor_begin,
            end=end,
        )
        available = {
            row[0]
            for row in self.storage.execute(
                f"DESCRIBE SELECT * FROM {source}"
            ).fetchall()
        }
        missing = sorted(set(columns) - available)
        if missing:
            raise ValueError(
                f"factor columns are missing from {self.factor_source}: {', '.join(missing)}"
            )
        self._drop_temp_relations(
            self._factor_batch_relation,
            "_evaluation_factor_wide",
        )
        industry_source = self.storage.partition_scan_sql(
            "industry_mapping",
            begin=factor_begin,
            end=end,
        )
        selected = ", ".join(
            f"CAST(f.{self.storage.identifier(column)} AS DOUBLE) AS "
            f"{self.storage.identifier(column)}"
            for column in columns
        )
        industry_column = self.storage.identifier(
            self.cfg.neutralization_industry_level
        )
        self.storage.execute(
            f"""
            CREATE TEMP TABLE _evaluation_factor_wide AS
            WITH quote_exposure AS (
                SELECT
                    date,
                    code,
                    log_market_cap
                FROM _evaluation_quotes
                WHERE CAST(date AS DATE)
                      BETWEEN CAST({self.storage.sql_literal(factor_begin)} AS DATE)
                          AND CAST({self.storage.sql_literal(end)} AS DATE)
            ),
            industry_exposure AS (
                SELECT
                    CAST(date AS DATE) AS date,
                    CAST(code AS VARCHAR) AS code,
                    NULLIF(CAST({industry_column} AS VARCHAR), '') AS industry_code
                FROM {industry_source}
                WHERE CAST(date AS DATE)
                      BETWEEN CAST({self.storage.sql_literal(factor_begin)} AS DATE)
                          AND CAST({self.storage.sql_literal(end)} AS DATE)
            )
            SELECT
                CAST(f.date AS DATE) AS date,
                CAST(f.code AS VARCHAR) AS code,
                q.log_market_cap,
                i.industry_code,
                {selected}
            FROM {source} f
            LEFT JOIN quote_exposure q
              ON q.date = CAST(f.date AS DATE)
             AND q.code = CAST(f.code AS VARCHAR)
            LEFT JOIN industry_exposure i
              ON i.date = CAST(f.date AS DATE)
             AND i.code = CAST(f.code AS VARCHAR)
            WHERE CAST(f.date AS DATE)
                  BETWEEN CAST({self.storage.sql_literal(factor_begin)} AS DATE)
                                AND CAST({self.storage.sql_literal(end)} AS DATE)
            """
        )
        identifiers = ", ".join(self.storage.identifier(column) for column in columns)
        self.storage.execute(
            f"""
            CREATE TEMP TABLE {self._factor_batch_relation} AS
            SELECT
                date,
                code,
                log_market_cap,
                industry_code,
                value_column AS factor_column,
                CAST(value AS DOUBLE) AS factor_value
            FROM (
                UNPIVOT (
                    SELECT date, code, log_market_cap, industry_code, {identifiers}
                    FROM _evaluation_factor_wide
                )
                ON {identifiers}
                INTO NAME value_column VALUE value
            )
            WHERE isfinite(CAST(value AS DOUBLE))
            """
        )
        self._drop_temp_relations("_evaluation_factor_wide")

    def _process_date_block(
        self,
        dates: tuple[str, ...],
        columns: Sequence[str],
    ) -> None:
        if not dates or not columns:
            return
        context_begin = self._context_begin(dates[0])
        try:
            self._create_forward_block(context_begin, dates[-1])
            if "correlation" in self.tasks:
                self._create_reference_block(context_begin, dates[-1])
            self._process_column_batch_with_retry(
                tuple(columns),
                dates=dates,
                context_begin=context_begin,
            )
        except Exception as exc:
            if not _is_oom(exc) or len(dates) <= 1:
                raise
            # Some smaller column retries may already have committed.  Remove
            # every result for this date/column block before retrying both
            # halves so an OOM can never create duplicates or partial dates.
            self._delete_block_results(dates, columns)
            midpoint = len(dates) // 2
            self._process_date_block(dates[:midpoint], columns)
            self._process_date_block(dates[midpoint:], columns)

    def _process_factor_date_block(
        self,
        dates: tuple[str, ...],
        columns: Sequence[str],
    ) -> None:
        """Build factor variants only for one bounded date block."""

        if not dates or not columns:
            return
        self._drop_temp_relations(
            self._factor_batch_relation,
            self._factor_variants_relation,
        )
        try:
            self._prepare_shared_forward_returns(dates[0], dates[-1])
            if "correlation" in self.tasks:
                self._prepare_reference_sources(self._context_begin(dates[0]), dates[-1])
            self._create_factor_batch(columns, dates[0], dates[-1])
            self._prepare_factor_variants(
                columns,
                process_begin=dates[0],
                process_end=dates[-1],
            )
            self._process_date_block(dates, columns)
        except Exception as exc:
            if not _is_oom(exc) or len(dates) <= 1:
                raise
            midpoint = len(dates) // 2
            self._process_factor_date_block(dates[:midpoint], columns)
            self._process_factor_date_block(dates[midpoint:], columns)
        finally:
            self._drop_temp_relations(
                self._factor_batch_relation,
                self._factor_variants_relation,
                self._forward_returns_relation,
                "_evaluation_quotes",
                "_forward_returns_block",
                "_reference_long",
                "_reference_ranked",
                "_reference_labels",
                *(spec[0] for spec in self._reference_specs),
            )
            self._reference_specs = ()
            self._reference_labels = ()


    def _delete_block_results(
        self,
        dates: Sequence[str],
        columns: Sequence[str],
    ) -> None:
        if not dates or not columns:
            return
        column_sql = _sql_list(self.storage, tuple(columns))
        begin = self.storage.sql_literal(dates[0])
        end = self.storage.sql_literal(dates[-1])
        for table in (
            "_evaluation_ic",
            "_evaluation_returns",
            "_evaluation_correlation",
        ):
            self.storage.execute(
                f"""
                DELETE FROM {table}
                WHERE factor_column IN ({column_sql})
                  AND date BETWEEN CAST({begin} AS DATE) AND CAST({end} AS DATE)
                """
            )

    def _create_forward_block(self, context_begin: str, block_end: str) -> None:
        self._drop_temp_relations("_forward_returns_block")
        block_begin = self.storage.sql_literal(context_begin)
        block_end_literal = self.storage.sql_literal(block_end)
        # A filtered view keeps block processing bounded without copying the
        # shared forward-return panel for every date chunk.
        self.storage.execute(
            f"""
            CREATE TEMP VIEW _forward_returns_block AS
            SELECT *
            FROM {self._forward_returns_relation}
            WHERE date BETWEEN CAST({block_begin} AS DATE)
                           AND CAST({block_end_literal} AS DATE)
            """
        )


    def _process_column_batch_with_retry(
        self,
        columns: tuple[str, ...],
        *,
        dates: tuple[str, ...],
        context_begin: str,
    ) -> None:
        try:
            self.storage.execute("BEGIN TRANSACTION")
            self._process_column_batch(
                columns,
                block_begin=dates[0],
                block_end=dates[-1],
                context_begin=context_begin,
            )
            self.storage.execute("COMMIT")
        except Exception as exc:
            try:
                self.storage.execute("ROLLBACK")
            except duckdb.Error:
                pass
            if not _is_oom(exc):
                raise
            if len(columns) > 1:
                midpoint = len(columns) // 2
                self._process_column_batch_with_retry(
                    columns[:midpoint],
                    dates=dates,
                    context_begin=context_begin,
                )
                self._process_column_batch_with_retry(
                    columns[midpoint:],
                    dates=dates,
                    context_begin=context_begin,
                )
                return
            raise

    def _prepare_factor_variants(
        self,
        columns: Sequence[str],
        *,
        process_begin: str,
        process_end: str,
        materialize: bool = True,
    ) -> None:
        """Materialize every required factor variant for the active block."""

        factor_columns = _sql_list(self.storage, tuple(columns))
        factor_context_begin = self._factor_context_begin(process_begin)
        rolling_preceding = max(self.cfg.rolling_mean_days - 1, 0)
        relation_kind = "TABLE" if materialize else "VIEW"
        self._drop_temp_relations(self._factor_variants_relation)
        self.storage.execute(
            f"""
            CREATE TEMP {relation_kind} {self._factor_variants_relation} AS
            WITH raw AS (
                SELECT
                    date,
                    code,
                    factor_column,
                    factor_value,
                    log_market_cap,
                    industry_code
                FROM {self._factor_batch_relation}
                WHERE factor_column IN ({factor_columns})
                  AND date BETWEEN CAST({self.storage.sql_literal(factor_context_begin)} AS DATE)
                                AND CAST({self.storage.sql_literal(process_end)} AS DATE)
            ),
            industry_stats AS (
                SELECT
                    factor_column,
                    date,
                    industry_code,
                    AVG(factor_value) AS factor_mean,
                    AVG(log_market_cap) AS cap_mean
                FROM raw
                WHERE industry_code IS NOT NULL
                  AND isfinite(log_market_cap)
                GROUP BY factor_column, date, industry_code
            ),
            centered AS (
                SELECT
                    raw.*,
                    stats.factor_mean,
                    stats.cap_mean,
                    raw.factor_value - stats.factor_mean AS factor_centered,
                    raw.log_market_cap - stats.cap_mean AS cap_centered
                FROM raw
                LEFT JOIN industry_stats stats
                  USING (factor_column, date, industry_code)
            ),
            beta_stats AS (
                SELECT
                    factor_column,
                    date,
                    SUM(factor_centered * cap_centered) AS covariance,
                    SUM(cap_centered * cap_centered) AS cap_variance
                FROM centered
                WHERE factor_mean IS NOT NULL
                  AND isfinite(cap_centered)
                GROUP BY factor_column, date
            ),
            neutralized_values AS (
                SELECT
                    centered.date,
                    centered.code,
                    centered.factor_column,
                    centered.factor_value,
                    CASE
                        WHEN centered.factor_mean IS NULL
                          OR NOT isfinite(centered.log_market_cap)
                        THEN NULL
                        WHEN beta.cap_variance > 0.0
                        THEN centered.factor_centered
                           - beta.covariance / beta.cap_variance
                             * centered.cap_centered
                        ELSE centered.factor_centered
                    END AS neutralized_value
                FROM centered
                LEFT JOIN beta_stats beta
                  USING (factor_column, date)
            ),
            rolling_values AS (
                SELECT
                    neutralized.*,
                    AVG(neutralized_value) OVER (
                        PARTITION BY factor_column, code
                        ORDER BY date
                        ROWS BETWEEN {rolling_preceding} PRECEDING AND CURRENT ROW
                    ) AS rolling_value
                FROM neutralized_values neutralized
            ),
            variants AS (
                SELECT
                    date,
                    code,
                    factor_column,
                    'raw' AS variant,
                    factor_value AS factor_value
                FROM raw
                UNION ALL
                SELECT
                    date,
                    code,
                    factor_column,
                    'neutralized' AS variant,
                    neutralized_value AS factor_value
                FROM rolling_values
                WHERE isfinite(neutralized_value)
                UNION ALL
                SELECT
                    date,
                    code,
                    factor_column,
                    'rolling_mean' AS variant,
                    rolling_value AS factor_value
                FROM rolling_values
                WHERE isfinite(rolling_value)
            )
            SELECT date, code, factor_column, variant, factor_value
            FROM variants
            WHERE isfinite(factor_value)
            """
        )

    def _create_factor_variants(
        self,
        columns: Sequence[str],
        *,
        block_begin: str,
        block_end: str,
    ) -> None:
        """Expose a bounded view of the precomputed factor variants."""

        factor_columns = _sql_list(self.storage, tuple(columns))
        factor_context_begin = self._factor_context_begin(block_begin)
        self.storage.execute(
            f"""
            CREATE TEMP VIEW _factor_long AS
            SELECT date, code, factor_column, variant, factor_value
            FROM {self._factor_variants_relation}
            WHERE factor_column IN ({factor_columns})
              AND date BETWEEN CAST({self.storage.sql_literal(factor_context_begin)} AS DATE)
                           AND CAST({self.storage.sql_literal(block_end)} AS DATE)
            """
        )

    def _create_ranked_positions(self) -> None:
        """Rank one eligible block once for IC, layers, and Top portfolios."""

        self.storage.execute(
            "CREATE TEMP TABLE _ranked_positions AS "
            + self._ranked_positions_query("_eligible_positions")
        )

    def _ranked_positions_query(self, eligible_relation: str) -> str:
        """Return the canonical one-pass ranking query for eligible positions."""

        quantiles = int(self.cfg.quantiles)
        fraction = float(self.cfg.top_percentile) / 100.0
        top_count_sql = (
            f"GREATEST(1, CAST(CEIL(sample_count * {fraction:.12g}) AS BIGINT))"
        )
        eligible = self.storage.identifier(eligible_relation)
        return f"""
            WITH ordered AS (
                SELECT
                    eligible.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY factor_column, variant, date
                        ORDER BY factor_value, code
                    ) AS factor_order,
                    COUNT(*) OVER (
                        PARTITION BY factor_column, variant, date
                    ) AS sample_count,
                    RANK() OVER (
                        PARTITION BY factor_column, variant, date
                        ORDER BY factor_value
                    ) AS factor_rank_base,
                    COUNT(*) OVER (
                        PARTITION BY factor_column, variant, date
                        ORDER BY factor_value
                        RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS factor_rank_end,
                    RANK() OVER (
                        PARTITION BY factor_column, variant, date
                        ORDER BY gross_security_return
                    ) AS return_rank_base,
                    COUNT(*) OVER (
                        PARTITION BY factor_column, variant, date
                        ORDER BY gross_security_return
                        RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS return_rank_end
                FROM {eligible} eligible
            )
            SELECT
                date,
                code,
                factor_column,
                variant,
                factor_value,
                portfolio_id,
                gross_security_return,
                CAST(
                    (factor_rank_base + factor_rank_end) / 2.0
                    AS DOUBLE
                ) AS factor_rank,
                CAST(
                    (return_rank_base + return_rank_end) / 2.0
                    AS DOUBLE
                ) AS return_rank,
                CAST(
                    CASE
                        WHEN factor_order <= (
                            FLOOR(sample_count / {quantiles}) + 1
                        ) * MOD(sample_count, {quantiles})
                        THEN CEIL(
                            factor_order / (
                                FLOOR(sample_count / {quantiles}) + 1
                            )
                        )
                        ELSE MOD(sample_count, {quantiles}) + CEIL(
                            (
                                factor_order
                                - (FLOOR(sample_count / {quantiles}) + 1)
                                  * MOD(sample_count, {quantiles})
                            ) / NULLIF(FLOOR(sample_count / {quantiles}), 0)
                        )
                    END AS INTEGER
                ) AS layer,
                sample_count,
                (
                    sample_count
                    - factor_rank_end
                    + (factor_order - factor_rank_base + 1)
                ) <= {top_count_sql} AS top_high_selected,
                factor_order <= {top_count_sql} AS top_low_selected
            FROM ordered
            """

    def _process_column_batch(
        self,
        columns: tuple[str, ...],
        *,
        block_begin: str,
        block_end: str,
        context_begin: str,
    ) -> None:
        if not self._active_factor_batch:
            raise RuntimeError("factor batch relation is not prepared")
        self._drop_temp_relations(
            "_factor_long",
            "_eligible_positions",
            "_ranked_positions",
            "_portfolio_positions",
        )
        self._create_factor_variants(
            columns,
            block_begin=block_begin,
            block_end=block_end,
        )
        self.storage.execute(
            """
            CREATE TEMP TABLE _eligible_positions AS
            SELECT
                factor.date,
                factor.code,
                factor.factor_column,
                factor.variant,
                factor.factor_value,
                returns.portfolio_id,
                returns.gross_security_return
            FROM _factor_long factor
            JOIN _forward_returns_block returns USING (date, code)
            WHERE isfinite(returns.gross_security_return)
            """
        )
        self._create_ranked_positions()
        if "ic" in self.tasks or "portfolio" in self.tasks:
            self._insert_ic(block_begin, block_end)
        if "portfolio" in self.tasks:
            self._create_position_tables()
            self._insert_returns(block_begin, block_end)
        if "correlation" in self.tasks:
            self._insert_correlations(block_begin, block_end)
