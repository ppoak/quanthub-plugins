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


    def _create_portfolio_targets(self, block_end: str) -> None:
        last = self.storage.sql_literal(block_end)
        self.storage.execute(
            f"""
            CREATE TEMP TABLE _portfolio_targets AS
            WITH targets AS (
                SELECT factor_column, variant, portfolio_id, layer, portfolio_kind,
                       date, calendar_seq, AVG(gross_security_return) AS gross_return,
                       COUNT(*) AS holdings,
                       SUM(target_weight * (1.0 + gross_security_return)) AS drift_total
                FROM _portfolio_positions
                JOIN _evaluation_calendar USING (date)
                WHERE date <= CAST({last} AS DATE)
                GROUP BY ALL
            )
            SELECT *, LAG(date) OVER schedule AS previous_date,
                      LAG(calendar_seq) OVER schedule AS previous_seq,
                      LAG(drift_total) OVER schedule AS previous_drift_total,
                      LEAD(calendar_seq) OVER schedule AS next_seq
            FROM targets
            WINDOW schedule AS (
                PARTITION BY factor_column, variant, portfolio_id, layer, portfolio_kind
                ORDER BY calendar_seq
            )
            """
        )

    def _create_gap_events(
        self, block_begin: str, block_end: str, signal_dates: str,
    ) -> None:
        """Expand only the missing rebalances and the first target after each gap."""

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
            CREATE TEMP TABLE _portfolio_gap_events AS
            WITH intervals AS (
                SELECT factor_column, variant, portfolio_id, layer, portfolio_kind,
                       date AS previous_date, calendar_seq AS target_seq,
                       COALESCE(next_seq, {last_seq + 1}) AS gap_end
                FROM _portfolio_targets
                WHERE calendar_seq + {horizon} < COALESCE(next_seq, {last_seq + 1})
                  AND (next_seq IS NULL OR next_seq >= {first_seq})
            ),
            dates AS (
                SELECT intervals.*, schedule.seq AS calendar_seq, FALSE AS has_target
                FROM intervals
                CROSS JOIN LATERAL range(target_seq + {horizon}, gap_end, {horizon}) AS schedule(seq)
                UNION ALL
                SELECT intervals.*, gap_end, TRUE
                FROM intervals
                WHERE gap_end <= {last_seq}
            ),
            slots AS (
                SELECT dates.*, calendar.date
                FROM dates JOIN _evaluation_calendar calendar USING (calendar_seq)
            ),
            actions AS (
                SELECT slots.*, {liquidation} AS requests_liquidation
                FROM slots
            ),
            exits AS (
                SELECT *, MIN(calendar_seq) FILTER (WHERE requests_liquidation) OVER (
                    PARTITION BY factor_column, variant, portfolio_id, layer, portfolio_kind, target_seq
                ) AS liquidation_seq
                FROM actions
            ),
            held AS (
                SELECT *, liquidation_seq IS NULL OR calendar_seq <= liquidation_seq AS invested
                FROM exits
            )
            SELECT factor_column, variant, portfolio_id, layer, portfolio_kind,
                   date, calendar_seq, has_target, requests_liquidation AS sell,
                   invested, previous_date, target_seq,
                   invested AND (calendar_seq > target_seq + {horizon}
                       OR (NOT has_target AND NOT requests_liquidation)) AS needs_mark
            FROM held
            WHERE calendar_seq >= {first_seq}
              AND (has_target OR invested)
            """
        )

    def _create_gap_marks(self, *, quotes_available: bool) -> None:
        """Value carried shares at the beginning and end of each missing interval."""

        lag = self.cfg.execution_lag_days
        horizon = self.cfg.horizon_days
        earliest, latest = self.storage.execute(
            f"""
            SELECT MIN(target_seq + {lag + horizon}), MAX(calendar_seq + {lag + horizon})
            FROM _portfolio_gap_events
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
                FROM _portfolio_gap_events events
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
        self._drop_temp_relations("_portfolio_targets", "_portfolio_gap_events", "_portfolio_gap_marks")
        self._create_portfolio_targets(block_end)
        self._create_gap_events(block_begin, block_end, signal_dates)
        self._create_gap_marks(quotes_available=quotes_available)
        first = self.storage.sql_literal(block_begin)
        last = self.storage.sql_literal(block_end)
        horizon = self.cfg.horizon_days
        needs_cutoff = self._block_requires_cutoff(block_begin, "returns")
        cutoff_join = (
            "JOIN _evaluation_cutoffs cutoff USING (factor_column)"
            if needs_cutoff
            else ""
        )
        cutoff_filter = (
            "WHERE layer_metrics.date >= cutoff.returns"
            if needs_cutoff
            else ""
        )
        self.storage.execute(
            f"""
            INSERT INTO _evaluation_returns
            WITH targets AS (
                SELECT * FROM _portfolio_targets
                WHERE date BETWEEN CAST({first} AS DATE) AND CAST({last} AS DATE)
            ),
            regular_previous AS (
                SELECT target.factor_column, target.date, target.variant, target.portfolio_id,
                       target.layer, target.portfolio_kind, previous.code,
                       previous.target_weight * (1.0 + previous.gross_security_return)
                           / NULLIF(target.previous_drift_total, 0.0) AS previous_weight
                FROM targets target
                JOIN _portfolio_positions previous
                  ON previous.factor_column = target.factor_column
                 AND previous.variant = target.variant
                 AND previous.date = target.previous_date
                 AND previous.portfolio_id = target.portfolio_id
                 AND previous.portfolio_kind = target.portfolio_kind
                 AND previous.layer = target.layer
                WHERE target.calendar_seq = target.previous_seq + {horizon}
            ),
            gap_previous_base AS (
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
                FROM _portfolio_gap_events dates
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
                WHERE dates.has_target AND dates.invested
            ),
            gap_previous_positions AS (
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
                FROM gap_previous_base
            ),
            -- Attach the rebalance date before matching securities; otherwise a
            -- join ordered by security first can pair holdings across many dates.
            previous_positions AS MATERIALIZED (
                SELECT * FROM regular_previous
                UNION ALL
                SELECT * FROM gap_previous_positions
            ),
            overlap AS (
                SELECT factor_column, date, variant, portfolio_id, layer, portfolio_kind,
                       SUM(LEAST(current.target_weight, previous.previous_weight)) AS common_weight
                FROM previous_positions previous
                JOIN _portfolio_positions current USING (
                    factor_column, date, variant, portfolio_id, layer, portfolio_kind, code
                )
                GROUP BY ALL
            ),
            target_metrics AS (
                SELECT
                    target.factor_column, target.date, target.variant, target.portfolio_id,
                    target.layer, target.portfolio_kind, target.gross_return, target.holdings,
                    target.holdings AS observations,
                    CASE
                        WHEN target.previous_seq IS NULL OR gap.invested IS FALSE THEN 1.0
                        ELSE LEAST(
                            2.0,
                            GREATEST(
                                0.0,
                                2.0 - 2.0 * COALESCE(overlap.common_weight, 0.0)
                            )
                        )
                    END AS turnover
                FROM targets target
                LEFT JOIN overlap USING (
                    factor_column, date, variant, portfolio_id, layer,
                    portfolio_kind
                )
                LEFT JOIN (SELECT * FROM _portfolio_gap_events WHERE has_target) gap USING (
                    factor_column, date, variant, portfolio_id, layer, portfolio_kind
                )
            ),
            metrics AS (
                SELECT * FROM target_metrics
                UNION ALL
                SELECT factor_column, date, variant, portfolio_id, layer, portfolio_kind,
                       SUM(end_value) / SUM(start_value) - 1.0, COUNT(*), 0, 0.0
                FROM _portfolio_gap_marks
                JOIN _portfolio_gap_events USING (
                    factor_column, date, variant, portfolio_id, layer, portfolio_kind
                )
                WHERE NOT has_target AND NOT sell
                GROUP BY factor_column, date, variant, portfolio_id, layer, portfolio_kind
                UNION ALL
                SELECT factor_column, date, variant, portfolio_id, layer, portfolio_kind,
                       0.0, 0, 0, 1.0
                FROM _portfolio_gap_events
                WHERE sell
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
            {cutoff_filter}
            """
        )
