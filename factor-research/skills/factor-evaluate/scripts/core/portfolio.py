from __future__ import annotations

from .shared import TRANSACTION_COST

class _PortfolioMixin:

    def _create_position_tables(self) -> None:
        self.storage.execute(
            """
            CREATE TEMP TABLE _portfolio_positions AS
            WITH layer_positions AS (
                SELECT
                    factor_column,
                    date,
                    variant,
                    portfolio_id,
                    layer,
                    0 AS portfolio_kind,
                    code,
                    gross_security_return,
                    1.0 / COUNT(*) OVER (
                        PARTITION BY factor_column, variant, date, portfolio_id, layer
                    ) AS target_weight
                FROM _ranked_positions
            ),
            top_high_positions AS (
                SELECT
                    factor_column,
                    date,
                    variant,
                    portfolio_id,
                    0 AS layer,
                    1 AS portfolio_kind,
                    code,
                    gross_security_return,
                    1.0 / COUNT(*) OVER (
                        PARTITION BY factor_column, variant, date, portfolio_id
                    ) AS target_weight
                FROM _ranked_positions
                WHERE top_high_selected
            ),
            top_low_positions AS (
                SELECT
                    factor_column,
                    date,
                    variant,
                    portfolio_id,
                    0 AS layer,
                    2 AS portfolio_kind,
                    code,
                    gross_security_return,
                    1.0 / COUNT(*) OVER (
                        PARTITION BY factor_column, variant, date, portfolio_id
                    ) AS target_weight
                FROM _ranked_positions
                WHERE top_low_selected
            )
            SELECT * FROM layer_positions
            UNION ALL
            SELECT * FROM top_high_positions
            UNION ALL
            SELECT * FROM top_low_positions
            """
        )


    def _insert_returns(self, block_begin: str, block_end: str) -> None:
        needs_cutoff = self._block_requires_cutoff(block_begin)
        cutoff_join = (
            "JOIN _evaluation_cutoffs cutoff USING (factor_column)"
            if needs_cutoff
            else ""
        )
        cutoff_filter = (
            "AND layer_metrics.date >= cutoff.cutoff"
            if needs_cutoff
            else ""
        )
        self.storage.execute(
            f"""
            INSERT INTO _evaluation_returns
            WITH rebalance_dates AS (
                SELECT
                    factor_column,
                    date,
                    variant,
                    portfolio_id,
                    layer,
                    portfolio_kind,
                    LAG(date) OVER (
                        PARTITION BY factor_column, variant, portfolio_id,
                                     portfolio_kind, layer
                        ORDER BY date
                    ) AS previous_date
                FROM (
                    SELECT DISTINCT
                        factor_column,
                        date,
                        variant,
                        portfolio_id,
                        layer,
                        portfolio_kind
                    FROM _portfolio_positions
                )
            ),
            previous_base AS (
                SELECT
                    dates.factor_column,
                    dates.date,
                    dates.variant,
                    dates.portfolio_id,
                    dates.layer,
                    dates.portfolio_kind,
                    previous.code,
                    previous.target_weight
                        * (1.0 + previous.gross_security_return) AS drift_value
                FROM rebalance_dates dates
                JOIN _portfolio_positions previous
                 ON previous.factor_column = dates.factor_column
                 AND previous.variant = dates.variant
                 AND previous.date = dates.previous_date
                 AND previous.portfolio_id = dates.portfolio_id
                 AND previous.portfolio_kind = dates.portfolio_kind
                 AND previous.layer IS NOT DISTINCT FROM dates.layer
            ),
            previous_positions AS (
                SELECT
                    factor_column,
                    date,
                    variant,
                    portfolio_id,
                    layer,
                    portfolio_kind,
                    code,
                    drift_value / NULLIF(
                        SUM(drift_value) OVER (
                            PARTITION BY factor_column, variant, date, portfolio_id,
                                         layer, portfolio_kind
                        ),
                        0.0
                    ) AS previous_weight
                FROM previous_base
            ),
            overlap AS (
                SELECT
                    current.factor_column,
                    current.date,
                    current.variant,
                    current.portfolio_id,
                    current.layer,
                    current.portfolio_kind,
                    SUM(LEAST(current.target_weight, previous.previous_weight))
                        AS common_weight
                FROM _portfolio_positions current
                JOIN previous_positions previous
                  ON previous.factor_column = current.factor_column
                 AND previous.variant = current.variant
                 AND previous.date = current.date
                 AND previous.portfolio_id = current.portfolio_id
                 AND previous.portfolio_kind = current.portfolio_kind
                 AND previous.layer IS NOT DISTINCT FROM current.layer
                 AND previous.code = current.code
                GROUP BY current.factor_column, current.date, current.variant,
                         current.portfolio_id, current.layer, current.portfolio_kind
            ),
            turnover AS (
                SELECT
                    dates.factor_column,
                    dates.date,
                    dates.variant,
                    dates.portfolio_id,
                    dates.layer,
                    dates.portfolio_kind,
                    CASE
                        WHEN dates.previous_date IS NULL THEN 1.0
                        ELSE LEAST(
                            2.0,
                            GREATEST(
                                0.0,
                                2.0 - 2.0 * COALESCE(overlap.common_weight, 0.0)
                            )
                        )
                    END AS turnover
                FROM rebalance_dates dates
                LEFT JOIN overlap USING (
                    factor_column, date, variant, portfolio_id, layer,
                    portfolio_kind
                )
            ),
            returns AS (
                SELECT
                    factor_column,
                    date,
                    variant,
                    portfolio_id,
                    layer,
                    portfolio_kind,
                    AVG(gross_security_return) AS gross_return,
                    COUNT(*) AS holdings
                FROM _portfolio_positions
                GROUP BY factor_column, date, variant, portfolio_id, layer,
                         portfolio_kind
            ),
            metrics AS (
                SELECT
                    returns.factor_column,
                    returns.date,
                    returns.variant,
                    returns.portfolio_id,
                    returns.layer,
                    returns.portfolio_kind,
                    returns.gross_return,
                    returns.holdings,
                    turnover.turnover
                FROM returns
                JOIN turnover USING (
                    factor_column, date, variant, portfolio_id, layer,
                    portfolio_kind
                )
            ),
            layer_metrics AS (
                SELECT *
                FROM metrics
                WHERE portfolio_kind = 0
            ),
            top_high_metrics AS (
                SELECT *
                FROM metrics
                WHERE portfolio_kind = 1
            ),
            top_low_metrics AS (
                SELECT *
                FROM metrics
                WHERE portfolio_kind = 2
            )
            SELECT
                layer_metrics.factor_column,
                layer_metrics.date,
                layer_metrics.variant,
                CAST(layer_metrics.portfolio_id AS INTEGER),
                CAST(layer_metrics.layer AS INTEGER),
                layer_metrics.gross_return,
                layer_metrics.gross_return
                    - layer_metrics.turnover * {TRANSACTION_COST:.12g},
                layer_metrics.turnover,
                layer_metrics.holdings,
                layer_metrics.holdings,
                top_high_metrics.gross_return,
                top_high_metrics.gross_return
                    - top_high_metrics.turnover * {TRANSACTION_COST:.12g},
                top_high_metrics.turnover,
                top_high_metrics.holdings,
                top_high_metrics.holdings,
                top_low_metrics.gross_return,
                top_low_metrics.gross_return
                    - top_low_metrics.turnover * {TRANSACTION_COST:.12g},
                top_low_metrics.turnover,
                top_low_metrics.holdings,
                top_low_metrics.holdings
            FROM layer_metrics
            JOIN top_high_metrics USING (factor_column, date, variant, portfolio_id)
            JOIN top_low_metrics USING (factor_column, date, variant, portfolio_id)
            {cutoff_join}
            WHERE layer_metrics.date
                  BETWEEN CAST({self.storage.sql_literal(block_begin)} AS DATE)
                      AND CAST({self.storage.sql_literal(block_end)} AS DATE)
              {cutoff_filter}
            """
        )
