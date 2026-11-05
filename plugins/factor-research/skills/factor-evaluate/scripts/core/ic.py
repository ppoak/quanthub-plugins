from __future__ import annotations


class _IcMixin:

    def _insert_ic(self, block_begin: str, block_end: str) -> None:
        needs_cutoff = self._block_requires_cutoff(block_begin)
        cutoff_join = (
            "JOIN _evaluation_cutoffs cutoff USING (factor_column)"
            if needs_cutoff
            else ""
        )
        cutoff_filter = "AND ranked.date >= cutoff.cutoff" if needs_cutoff else ""
        self.storage.execute(
            f"""
            INSERT INTO _evaluation_ic
            WITH metrics AS (
                SELECT
                    ranked.factor_column,
                    ranked.date,
                    ranked.variant,
                    corr(ranked.factor_rank, ranked.return_rank) AS ic,
                    COUNT(*) AS observations
                FROM _ranked_positions ranked
                {cutoff_join}
                WHERE ranked.date
                      BETWEEN CAST({self.storage.sql_literal(block_begin)} AS DATE)
                          AND CAST({self.storage.sql_literal(block_end)} AS DATE)
                  {cutoff_filter}
                GROUP BY ranked.factor_column, ranked.date, ranked.variant
            )
            SELECT factor_column, date, variant, ic, observations
            FROM metrics
            WHERE observations >= 3
              AND isfinite(ic)
            """
        )
