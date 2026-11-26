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


    def _create_portfolio_events(
        self, block_begin: str, block_end: str, signal_dates: str,
    ) -> None:
        """Track each staggered portfolio on its scheduled rebalance dates."""

        first_seq = self._calendar_index[block_begin] + 1
        last_seq = self._calendar_index[block_end] + 1
        horizon = self.cfg.horizon_days
        liquidation = (
            f"""
            NOT has_target AND NOT EXISTS (
                SELECT 1 FROM ({signal_dates}) valid
                WHERE valid.factor_column = slots.factor_column AND valid.date = slots.date
            )
            """
            if self.cfg.missing_signal == "liquidate"
            else "FALSE"
        )
        self.storage.execute(
            f"""
            CREATE TEMP TABLE _portfolio_events AS
            WITH targets AS (
                SELECT DISTINCT factor_column, variant, portfolio_id, layer,
                                portfolio_kind, calendar_seq
                FROM _portfolio_positions
                JOIN _evaluation_calendar USING (date)
            ),
            starts AS (
                SELECT factor_column, variant, portfolio_id, layer, portfolio_kind,
                       MIN(calendar_seq) AS first_seq
                FROM targets
                GROUP BY ALL
            ),
            slots AS (
                SELECT starts.factor_column, starts.variant, starts.portfolio_id,
                       starts.layer, starts.portfolio_kind, calendar.date,
                       calendar.calendar_seq, targets.calendar_seq IS NOT NULL AS has_target
                FROM starts
                CROSS JOIN LATERAL range(starts.first_seq, {last_seq + 1}, {horizon})
                    AS schedule(seq)
                JOIN _evaluation_calendar calendar ON calendar.calendar_seq = schedule.seq
                LEFT JOIN targets USING (
                    factor_column, variant, portfolio_id, layer, portfolio_kind, calendar_seq
                )
            ),
            actions AS (
                SELECT slots.*, {liquidation} AS requests_liquidation
                FROM slots
            ),
            history AS (
                SELECT actions.*,
                       MAX(calendar_seq) FILTER (WHERE has_target) OVER before_trade AS target_seq,
                       MAX(calendar_seq) FILTER (WHERE requests_liquidation) OVER before_trade
                           AS liquidation_seq
                FROM actions
                WINDOW before_trade AS (
                    PARTITION BY factor_column, variant, portfolio_id, layer, portfolio_kind
                    ORDER BY calendar_seq ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                )
            ),
            held AS (
                SELECT history.*,
                       target_seq IS NOT NULL
                           AND (liquidation_seq IS NULL OR target_seq > liquidation_seq) AS invested
                FROM history
            )
            SELECT held.factor_column, held.variant, held.portfolio_id, held.layer,
                   held.portfolio_kind, held.date, held.calendar_seq, held.has_target,
                   held.requests_liquidation AS sell,
                   held.invested, prior.date AS previous_date, held.target_seq,
                   invested AND (held.calendar_seq > target_seq + {horizon}
                       OR (NOT has_target AND NOT requests_liquidation)) AS needs_mark
            FROM held
            LEFT JOIN _evaluation_calendar prior
                ON held.invested AND prior.calendar_seq = held.target_seq
            WHERE held.calendar_seq >= {first_seq}
              AND (held.has_target OR held.invested)
            """
        )

    def _create_gap_marks(self, *, quotes_available: bool) -> None:
        """Value carried shares at the beginning and end of each missing interval."""

        lag = self.cfg.execution_lag_days
        horizon = self.cfg.horizon_days
        earliest, latest = self.storage.execute(
            f"""
            SELECT MIN(target_seq + {lag + horizon}), MAX(calendar_seq + {lag + horizon})
            FROM _portfolio_events
            WHERE needs_mark
            """
        ).fetchone()
        if earliest is None:
            self.storage.execute(
                """
                CREATE TEMP TABLE _portfolio_gap_marks AS
                SELECT factor_column, variant, portfolio_id, layer, portfolio_kind, date, code,
                       0.0::DOUBLE AS start_value, 0.0::DOUBLE AS end_value
                FROM _portfolio_positions WHERE FALSE
                """
            )
            return
        begin_date = self.quote_dates[earliest - 1]
        end_date = self.quote_dates[latest - 1]
        low = self.storage.sql_literal(begin_date)
        high = self.storage.sql_literal(end_date)
        if quotes_available:
            prices = "_evaluation_quotes"
        else:
            source = self.storage.partition_scan_sql("quotes_day", begin=begin_date, end=end_date)
            price = self.storage.identifier(self.cfg.price_col)
            prices = f"(SELECT CAST(date AS DATE) AS date, code, {price} AS price FROM {source})"
        self.storage.execute(
            f"""
            CREATE TEMP TABLE _portfolio_gap_marks AS
            WITH marks AS (
                SELECT events.factor_column, events.variant, events.portfolio_id, events.layer,
                       events.portfolio_kind, events.date, events.sell, previous.code,
                       previous.target_weight * (1.0 + previous.gross_security_return)
                           AS anchor_value,
                       anchor.date AS anchor_date, entry.date AS entry_date,
                       CASE WHEN NOT events.has_target AND NOT events.sell
                            THEN finish.date ELSE entry.date END AS finish_date
                FROM _portfolio_events events
                JOIN _portfolio_positions previous
                  ON previous.factor_column = events.factor_column
                 AND previous.variant = events.variant
                 AND previous.portfolio_id = events.portfolio_id
                 AND previous.layer = events.layer
                 AND previous.portfolio_kind = events.portfolio_kind
                 AND previous.date = events.previous_date
                JOIN _evaluation_calendar anchor ON anchor.calendar_seq = events.target_seq + {lag + horizon}
                JOIN _evaluation_calendar entry ON entry.calendar_seq = events.calendar_seq + {lag}
                JOIN _evaluation_calendar finish ON finish.calendar_seq = events.calendar_seq + {lag + horizon}
                WHERE events.needs_mark
            ),
            prices AS (
                SELECT date, code, price
                FROM {prices}
                SEMI JOIN (SELECT DISTINCT code FROM marks) USING (code)
                WHERE date BETWEEN CAST({low} AS DATE) AND CAST({high} AS DATE)
                  AND isfinite(price) AND price > 0.0
            )
            SELECT marks.factor_column, marks.variant, marks.portfolio_id, marks.layer,
                   marks.portfolio_kind, marks.date, marks.code,
                   CASE WHEN marks.sell AND start_quote.date <> marks.entry_date
                        THEN error('Missing liquidation price for ' || marks.code || ' on ' || marks.entry_date)
                        ELSE marks.anchor_value * start_quote.price / anchor.price END AS start_value,
                   marks.anchor_value * finish_quote.price / anchor.price AS end_value
            FROM marks
            JOIN prices anchor ON anchor.date = marks.anchor_date AND anchor.code = marks.code
            ASOF JOIN prices start_quote ON marks.code = start_quote.code AND marks.entry_date >= start_quote.date
            ASOF JOIN prices finish_quote ON marks.code = finish_quote.code AND marks.finish_date >= finish_quote.date
            """
        )

    def _insert_returns(
        self, block_begin: str, block_end: str, *, signal_dates: str, quotes_available: bool = True,
    ) -> None:
        self._drop_temp_relations("_portfolio_events", "_portfolio_gap_marks")
        self._create_portfolio_events(block_begin, block_end, signal_dates)
        self._create_gap_marks(quotes_available=quotes_available)
        needs_cutoff = self._block_requires_cutoff(block_begin, "returns")
        cutoff_join = (
            "JOIN _evaluation_cutoffs cutoff USING (factor_column)"
            if needs_cutoff
            else ""
        )
        cutoff_filter = (
            "AND layer_metrics.date >= cutoff.returns"
            if needs_cutoff
            else ""
        )
        self.storage.execute(
            f"""
            INSERT INTO _evaluation_returns
            WITH previous_base AS (
                SELECT
                    dates.factor_column,
                    dates.date,
                    dates.variant,
                    dates.portfolio_id,
                    dates.layer,
                    dates.portfolio_kind,
                    previous.code,
                    COALESCE(marks.start_value,
                        previous.target_weight * (1.0 + previous.gross_security_return)) AS drift_value
                FROM _portfolio_events dates
                JOIN _portfolio_positions previous
                 ON previous.factor_column = dates.factor_column
                 AND previous.variant = dates.variant
                 AND previous.date = dates.previous_date
                 AND previous.portfolio_id = dates.portfolio_id
                 AND previous.portfolio_kind = dates.portfolio_kind
                 AND previous.layer = dates.layer
                LEFT JOIN _portfolio_gap_marks marks
                  ON marks.factor_column = dates.factor_column
                 AND marks.variant = dates.variant
                 AND marks.portfolio_id = dates.portfolio_id
                 AND marks.layer = dates.layer
                 AND marks.portfolio_kind = dates.portfolio_kind
                 AND marks.date = dates.date
                 AND marks.code = previous.code
                WHERE dates.has_target
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
                 AND previous.layer = current.layer
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
                        WHEN dates.sell THEN 1.0
                        WHEN NOT dates.has_target THEN 0.0
                        WHEN NOT dates.invested THEN 1.0
                        ELSE LEAST(
                            2.0,
                            GREATEST(
                                0.0,
                                2.0 - 2.0 * COALESCE(overlap.common_weight, 0.0)
                            )
                        )
                    END AS turnover
                FROM _portfolio_events dates
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
                    COUNT(*) AS holdings,
                    COUNT(*) AS observations
                FROM _portfolio_positions
                GROUP BY factor_column, date, variant, portfolio_id, layer,
                         portfolio_kind
                UNION ALL
                SELECT factor_column, date, variant, portfolio_id, layer, portfolio_kind,
                       SUM(end_value) / SUM(start_value) - 1.0, COUNT(*), 0
                FROM _portfolio_gap_marks
                JOIN _portfolio_events USING (
                    factor_column, date, variant, portfolio_id, layer, portfolio_kind
                )
                WHERE NOT has_target AND NOT sell
                GROUP BY factor_column, date, variant, portfolio_id, layer, portfolio_kind
                UNION ALL
                SELECT factor_column, date, variant, portfolio_id, layer, portfolio_kind,
                       0.0, 0, 0
                FROM _portfolio_events
                WHERE sell
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
                    returns.observations,
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
                layer_metrics.observations,
                top_high_metrics.gross_return,
                top_high_metrics.gross_return
                    - top_high_metrics.turnover * {TRANSACTION_COST:.12g},
                top_high_metrics.turnover,
                top_high_metrics.holdings,
                top_high_metrics.observations,
                top_low_metrics.gross_return,
                top_low_metrics.gross_return
                    - top_low_metrics.turnover * {TRANSACTION_COST:.12g},
                top_low_metrics.turnover,
                top_low_metrics.holdings,
                top_low_metrics.observations
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
