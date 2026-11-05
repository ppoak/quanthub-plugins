from __future__ import annotations

from .shared import FactorPath, Sequence, _chunks

class _CorrelationMixin:

    def _unpivot_source_sql(
        self,
        source: str,
        columns: Sequence[str],
        begin: str,
        end: str,
    ) -> str:
        selected = ", ".join(
            f"CAST({self.storage.identifier(column)} AS DOUBLE) "
            f"AS {self.storage.identifier(column)}"
            for column in columns
        )
        identifiers = ", ".join(self.storage.identifier(column) for column in columns)
        return f"""
        UNPIVOT (
            SELECT
                CAST(date AS DATE) AS date,
                CAST(code AS VARCHAR) AS code,
                {selected}
            FROM {source}
            WHERE CAST(date AS DATE)
                  BETWEEN CAST({self.storage.sql_literal(begin)} AS DATE)
                      AND CAST({self.storage.sql_literal(end)} AS DATE)
        )
        ON {identifiers}
        INTO NAME value_column VALUE value
        """


    def _prepare_reference_sources(
        self,
        begin: str,
        end: str,
        *,
        materialize: bool = True,
    ) -> None:
        """Prepare reference columns for the active date block.

        The sequential engine materializes only the bounded source window and
        releases it after the block.  Isolated correlation workers can keep
        their bounded sources as parquet-backed views instead.
        """

        self._drop_temp_relations(
            "_reference_long",
            "_reference_ranked",
            "_reference_labels",
            *(spec[0] for spec in self._reference_specs),
        )
        self._reference_specs = ()
        self._reference_labels = ()
        if not self.reference_factors:
            return
        grouped: dict[str, list[str]] = {}
        for value in self.reference_factors:
            path = FactorPath.parse(value)
            grouped.setdefault(path.table, []).append(path.column)
        specs: list[tuple[str, tuple[str, ...], tuple[str, ...], tuple[int, ...]]] = []
        for table_index, (table, columns) in enumerate(grouped.items()):
            try:
                source = self.storage.partition_scan_sql(
                    table,
                    begin=begin,
                    end=end,
                )
            except FileNotFoundError:
                # A reference table may start later than the requested factor
                # range.  Keep the empty/NULL semantics of the prior wildcard
                # scan while using concrete partitions whenever available.
                source = self.storage._table_scan_source(table)
            available = {
                row[0]
                for row in self.storage.execute(
                    f"DESCRIBE SELECT * FROM {source}"
                ).fetchall()
            }
            missing = sorted(set(columns) - available)
            if missing:
                raise ValueError(
                    f"reference factor columns are missing from {table}: {', '.join(missing)}"
                )
            aliases = tuple(
                f"ref_{table_index}_{column_index}"
                for column_index in range(len(columns))
            )
            labels = tuple(f"{table}.{column}" for column in columns)
            relation = f"_evaluation_reference_wide_{table_index}"
            selected = ", ".join(
                f"CAST({self.storage.identifier(column)} AS DOUBLE) AS "
                f"{self.storage.identifier(alias)}"
                for column, alias in zip(columns, aliases, strict=True)
            )
            relation_kind = "TABLE" if materialize else "VIEW"
            # Input order is not part of any evaluation semantic: every
            # downstream rank/correlation query partitions explicitly by
            # date/reference id.  A global historical sort here creates a
            # large spill before the actual bounded date blocks run, which
            # is particularly expensive for wide reference factor panels.
            order_sql = ""
            self.storage.execute(
                f"""
                CREATE TEMP {relation_kind} {relation} AS
                SELECT
                    CAST(date AS DATE) AS date,
                    CAST(code AS VARCHAR) AS code,
                    {selected}
                FROM {source}
                WHERE CAST(date AS DATE)
                      BETWEEN CAST({self.storage.sql_literal(begin)} AS DATE)
                          AND CAST({self.storage.sql_literal(end)} AS DATE)
                {order_sql}
                """
            )
            start_id = sum(len(item[2]) for item in specs)
            reference_ids = tuple(
                range(start_id, start_id + len(labels))
            )
            specs.append((relation, aliases, labels, reference_ids))
        self._reference_specs = tuple(specs)
        self._reference_labels = tuple(
            (reference_id, label)
            for _, _, labels, reference_ids in self._reference_specs
            for reference_id, label in zip(reference_ids, labels, strict=True)
        )
        self.storage.execute(
            "CREATE TEMP TABLE _reference_labels (reference_id INTEGER, reference_factor VARCHAR)"
        )
        self.storage.executemany(
            "INSERT INTO _reference_labels VALUES (?, ?)",
            self._reference_labels,
        )


    def _create_reference_block(
        self,
        begin: str,
        end: str,
        *,
        materialize: bool = True,
    ) -> None:
        """Expose only the current date block as a long reference view."""

        self._drop_temp_relations("_reference_long", "_reference_ranked")
        if not self._reference_specs:
            self.storage.execute(
                """
                CREATE TEMP TABLE _reference_long (
                    date DATE,
                    code VARCHAR,
                    reference_id INTEGER,
                    reference_value DOUBLE
                )
                """
            )
            self.storage.execute(
                """
                CREATE TEMP TABLE _reference_ranked (
                    date DATE,
                    code VARCHAR,
                    reference_id INTEGER,
                    reference_rank DOUBLE
                )
                """
            )
            return
        sources: list[str] = []
        for relation, aliases, _labels, reference_ids in self._reference_specs:
            unpivot = self._unpivot_source_sql(relation, aliases, begin, end)
            cases = " ".join(
                f"WHEN {self.storage.sql_literal(alias)} THEN "
                f"{reference_id}"
                for alias, reference_id in zip(aliases, reference_ids, strict=True)
            )
            sources.append(
                f"""
                SELECT
                    date,
                    code,
                    CASE value_column {cases} END AS reference_id,
                    value AS reference_value
                FROM (
                    {unpivot}
                )
                WHERE isfinite(value)
                """
            )
        self.storage.execute(
            "CREATE TEMP VIEW _reference_long AS " + "\nUNION ALL\n".join(sources)
        )
        relation_kind = "TABLE" if materialize else "VIEW"
        self.storage.execute(
            f"""
            CREATE TEMP {relation_kind} _reference_ranked AS
            WITH eligible_references AS (
                SELECT
                    reference.date,
                    reference.code,
                    reference.reference_id,
                    reference.reference_value
                FROM _reference_long reference
                JOIN (
                    SELECT DISTINCT date, code
                    FROM _forward_returns_block
                ) eligible USING (date, code)
            ),
            ordered AS (
                SELECT
                    eligible_references.*,
                    RANK() OVER (
                        PARTITION BY date, reference_id
                        ORDER BY reference_value
                    ) AS reference_rank_base,
                    COUNT(*) OVER (
                        PARTITION BY date, reference_id
                        ORDER BY reference_value
                        RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS reference_rank_end
                FROM eligible_references
            )
            SELECT
                date,
                code,
                reference_id,
                CAST(
                    (reference_rank_base + reference_rank_end) / 2.0
                    AS DOUBLE
                ) AS reference_rank
            FROM ordered
            """
        )


    def _insert_correlations(self, block_begin: str, block_end: str) -> None:
        self._insert_correlations_from(
            "_ranked_positions",
            block_begin=block_begin,
            block_end=block_end,
        )


    def _insert_correlations_from(
        self,
        candidate_relation: str,
        *,
        block_begin: str,
        block_end: str,
    ) -> None:
        if not self.reference_factors:
            return
        candidates = self.storage.identifier(candidate_relation)
        needs_cutoff = self._block_requires_cutoff(block_begin)
        cutoff_join = (
            "JOIN _evaluation_cutoffs cutoff USING (factor_column)"
            if needs_cutoff
            else ""
        )
        cutoff_filter = "AND candidate.date >= cutoff.cutoff" if needs_cutoff else ""
        reference_ids = tuple(reference_id for reference_id, _ in self._reference_labels)
        batches = tuple(
            tuple(batch)
            for batch in _chunks(reference_ids, self._reference_correlation_batch_size)
        )
        def correlation_query(batch: Sequence[int]) -> str:
            reference_filter = ", ".join(str(int(value)) for value in batch)
            return f"""
                WITH metrics AS (
                    SELECT
                        candidate.factor_column,
                        candidate.date,
                        candidate.variant,
                        reference.reference_id,
                        corr(
                            candidate.factor_rank,
                            reference.reference_rank
                        ) AS correlation,
                        COUNT(*) AS observations
                    FROM {candidates} candidate
                    JOIN _reference_ranked reference USING (date, code)
                    {cutoff_join}
                    WHERE reference.reference_id IN ({reference_filter})
                      AND candidate.date
                          BETWEEN CAST({self.storage.sql_literal(block_begin)} AS DATE)
                              AND CAST({self.storage.sql_literal(block_end)} AS DATE)
                      {cutoff_filter}
                    GROUP BY factor_column, date, variant, reference_id
                )
                SELECT
                    metrics.factor_column,
                    metrics.date,
                    metrics.variant,
                    labels.reference_factor,
                    metrics.correlation,
                    metrics.observations
                FROM metrics
                JOIN _reference_labels labels USING (reference_id)
                WHERE metrics.observations >= 3
                  AND isfinite(metrics.correlation)
                """

        for batch in batches:
            self.storage.execute(
                "INSERT INTO _evaluation_correlation "
                + correlation_query(batch)
            )
