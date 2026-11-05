"""Coordinate bounded factor evaluation across state, compute, and result modules."""

from __future__ import annotations

from dataclasses import replace
from time import monotonic


from .compute import _EvaluationComputeMixin
from factor_protocol import resolve_data_root, resolve_factor_data_root
from .results import _EvaluationResultsMixin
from .shared import (
    CORRELATION_COLUMNS,
    DEFAULT_EVALUATION_BEGIN,
    DEFAULT_NEUTRALIZATION_INDUSTRY_LEVEL,
    DEFAULT_ROLLING_MEAN_DAYS,
    DEFAULT_TRAIN_END,
    DEFAULT_VALIDATION_END,
    EVALUATION_FILES,
    EVALUATION_VARIANTS,
    IC_COLUMNS,
    RETURNS_COLUMNS,
    SINGLE_COLUMN_DATE_BATCH_DAYS,
    Any,
    EvaluationConfig,
    EvaluationSettings,
    FactorPath,
    FactorStorage,
    Mapping,
    Path,
    Sequence,
    _chunks,
    _ColumnState,
    _partition_dates,
    read_config,
    print_progress,
    replace_column_directory,
    resolve_output_root,
    shutil,
    validate_identifier,
)
from .state import _EvaluationStateMixin

class _EvaluationEngine(
    _EvaluationStateMixin, _EvaluationComputeMixin, _EvaluationResultsMixin
):
    def __init__(
        self,
        *,
        storage: FactorStorage,
        cfg: EvaluationConfig,
        project_root: Path,
        factor_source: str,
        factor_columns: Sequence[str],
        reference_factors: Sequence[str],
    ) -> None:
        self.storage = storage
        self.cfg = cfg
        self.tasks = cfg.tasks
        self.settings = cfg.settings or EvaluationSettings()
        self.project_root = project_root
        self.factor_source = validate_identifier(factor_source, "factor_source")
        self.factor_columns = tuple(
            validate_identifier(column, "factor_column") for column in factor_columns
        )
        self.reference_factors = tuple(
            dict.fromkeys(FactorPath.parse(value).label for value in reference_factors)
        )
        self.data_root = Path(cfg.data_root)
        if not self.data_root.is_absolute():
            self.data_root = project_root / self.data_root
        self.output_root = resolve_output_root(
            cfg.output_dir, project_root=project_root
        )
        if cfg.factor_name is not None:
            self.output_root = self.output_root / cfg.factor_name
        self.factor_range = storage.factor_date_range()
        if self.factor_range is None:
            raise ValueError(f"factor data is unavailable: {self.factor_source}")
        self.begin = cfg.begin or self.factor_range[0]
        self.requested_end = cfg.end or self.factor_range[1]
        if self.begin > self.requested_end:
            raise ValueError("evaluation begin must not be later than end")
        self.quote_dates = _partition_dates(self.data_root / "quotes_day")
        self.signal_dates = [
            value
            for value in self.quote_dates
            if self.begin <= value <= self.requested_end
        ]
        if not self.signal_dates:
            raise ValueError("evaluation range contains no trading dates")
        self.end = self.signal_dates[-1]
        if not self.begin <= self.cfg.train_end < self.cfg.validation_end < self.end:
            raise ValueError(
                "evaluation dates must satisfy "
                "begin <= train_end < validation_end < last trading date"
            )
        self._calendar_index = {
            value: index for index, value in enumerate(self.quote_dates)
        }
        self._anchor_signal_index = self._calendar_index[self.signal_dates[0]]
        self._manifest_cache: dict[tuple[str, str | None], dict[str, Any]] = {}
        self._states: tuple[_ColumnState, ...] = ()
        self.column_batch_size = self.settings.default_column_batch_size
        self.date_batch_days = self.settings.default_date_batch_days
        # The expensive inputs are materialized only for the active date block.
        # Cleanup after each block bounds the peak panel size on long histories.
        self._factor_batch_relation = "_evaluation_factor_batch"
        self._factor_variants_relation = "_evaluation_factor_variants"
        self._forward_returns_relation = "_evaluation_forward_returns"
        self._active_factor_batch: tuple[str, ...] = ()
        self._reference_specs: tuple[
            tuple[str, tuple[str, ...], tuple[str, ...], tuple[int, ...]], ...
        ] = ()
        self._reference_labels: tuple[tuple[int, str], ...] = ()
        self._reference_refresh_active = False
        self._reference_correlation_batch_size = (
            self.settings.reference_correlation_batch_size
        )

    def run(self) -> tuple[Path, list[tuple[Path, Path]]]:
        label = f"Evaluate {self.cfg.factor_name or self.cfg.factor_data} [{', '.join(self.factor_columns)}]"
        print(f"{label} | phase=preparing data", flush=True)
        self._validate_quote_schema()
        self._validate_industry_schema()
        current_factor_manifest = self._manifest(self.factor_source, self.end)
        self._states = self._column_states(current_factor_manifest)
        self._create_calendar()
        self.column_batch_size, self.date_batch_days = self._batch_sizes()
        self._create_stage_tables()
        process_begin = min(state.cutoff for state in self._states)
        # Do not spend date-block work on the leading part of a requested
        # range where the factor source has no materialized partitions.
        # Evaluation still records the requested begin date in config, while
        # metrics naturally start at the first available factor date.
        effective_begin = max(process_begin, self.factor_range[0])
        process_dates = [date for date in self.signal_dates if date >= effective_begin]
        if process_dates:
            blocks = tuple(
                tuple(block) for block in _chunks(process_dates, self.date_batch_days)
            )
            started = monotonic()
            completed = 0
            total = len(process_dates) * len(self.factor_columns)
            # Prepare factor variants per date block.  Keeping the complete
            # factor history in one temporary relation makes the rolling and
            # neutralized panels exceed the DuckDB memory limit on long runs.
            for batch in _chunks(self.factor_columns, self.column_batch_size):
                batch_columns = tuple(batch)
                self._active_factor_batch = batch_columns
                try:
                    for block in blocks:
                        print(f"{label} | dates={block[0]} -> {block[-1]} | "
                              f"columns={', '.join(batch_columns)}", flush=True)
                        self._process_factor_date_block(tuple(block), batch_columns)
                        completed += len(block) * len(batch_columns)
                        print_progress(f"{label} date/column progress", completed, total, started)
                finally:
                    self._drop_temp_relations(
                        self._factor_batch_relation,
                        self._factor_variants_relation,
                    )
                    self._active_factor_batch = ()
        if any(state.reference_refresh for state in self._states):
            print(f"{label} | phase=refreshing reference correlations", flush=True)
            self._run_reference_refresh()
        self._drop_temp_relations(
            *(spec[0] for spec in self._reference_specs),
            "_reference_long",
            "_reference_ranked",
            "_reference_labels",
            "_evaluation_quotes",
        )
        self._reference_specs = ()
        self._reference_labels = ()
        print(f"{label} | phase=building curves and checking artifacts", flush=True)
        if "portfolio" in self.tasks:
            self._create_nav_results()
        staged_columns = [
            (self._stage_column(state), state.output_dir)
            for state in self._states
        ]
        result = self._states[0].output_dir if len(self._states) == 1 else self.output_root
        return result, staged_columns


def run_evaluation(cfg: EvaluationConfig) -> Path:
    """Evaluate explicit data columns without package discovery or lifecycle checks."""

    project_root = (
        Path(cfg.project_root) if cfg.project_root is not None else Path.cwd()
    )
    settings = cfg.settings or EvaluationSettings.from_project_root(project_root)
    settings.validate()
    cfg = replace(
        cfg,
        settings=settings,
        data_root=resolve_data_root(
            project_root=project_root,
            data_root=cfg.data_root,
        ),
        factor_data=(
            (project_root / cfg.factor_data).resolve()
            if cfg.factor_data is not None else None
        ),
    )
    cfg.validate()
    factor_source = cfg.storage_factor_name or cfg.factor_name or "_factor_data"
    factor_columns = tuple(cfg.factor_columns or ())
    if not factor_columns:
        raise ValueError("core evaluation requires explicit factor_columns")
    reference_factors = tuple(cfg.reference_factors or ())
    if set(cfg.tasks) != {"ic", "portfolio", "correlation"}:
        cfg = replace(cfg, intra_column_workers=1)

    if cfg.intra_column_workers > 1 and set(cfg.tasks) == {"ic", "portfolio", "correlation"}:
        if len(factor_columns) != 1:
            raise ValueError(
                "intra-column parallel evaluation requires exactly one factor column"
            )
        if not cfg.rebuild:
            raise ValueError("intra-column parallel evaluation requires a full rebuild")
        if not reference_factors:
            raise ValueError(
                "intra-column parallel evaluation requires reference factors"
            )
        from .parallel import run_parallel_single_column_evaluation

        return run_parallel_single_column_evaluation(
            cfg=cfg,
            project_root=project_root,
            factor_source=factor_source,
            factor_columns=factor_columns,
            reference_factors=reference_factors,
        )

    data_root = Path(cfg.data_root)
    temp_directory = Path(cfg.temp_directory)
    if not temp_directory.is_absolute():
        temp_directory = project_root / temp_directory
    temp_directory = temp_directory / factor_source
    storage = FactorStorage(
        project_root=project_root,
        data_root=data_root,
        factor_name=factor_source,
        factor_root=cfg.factor_data,
        threads=cfg.threads,
        memory_limit=cfg.memory_limit,
        temp_directory=temp_directory,
        duckdb_settings={"preserve_insertion_order": False},
    )
    try:
        engine = _EvaluationEngine(
            storage=storage,
            cfg=cfg,
            project_root=project_root,
            factor_source=factor_source,
            factor_columns=factor_columns,
            reference_factors=reference_factors,
        )
        result, staged_columns = engine.run()
    except Exception:
        storage.close()
        raise
    storage.close()
    replaced: list[Path] = []
    try:
        print(f"Evaluate {cfg.factor_name or cfg.factor_data} | phase=committing metrics", flush=True)
        for staging, destination in staged_columns:
            replace_column_directory(staging, destination)
            replaced.append(destination)
    except Exception:
        for staging, _ in staged_columns:
            if staging.exists():
                shutil.rmtree(staging)
        raise
    return result


__all__ = [
    "CORRELATION_COLUMNS",
    "DEFAULT_EVALUATION_BEGIN",
    "DEFAULT_NEUTRALIZATION_INDUSTRY_LEVEL",
    "DEFAULT_ROLLING_MEAN_DAYS",
    "DEFAULT_TRAIN_END",
    "DEFAULT_VALIDATION_END",
    "EVALUATION_FILES",
    "EVALUATION_VARIANTS",
    "EvaluationConfig",
    "EvaluationSettings",
    "IC_COLUMNS",
    "RETURNS_COLUMNS",
    "SINGLE_COLUMN_DATE_BATCH_DAYS",
    "run_evaluation",
]
