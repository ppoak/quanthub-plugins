"""Factor-package orchestration: discovery, validation gate, then generic evaluation."""
from dataclasses import replace
from .engine import EvaluationConfig, EvaluationSettings, run_evaluation
from .shared import (Path, Sequence, Any, Mapping, FactorPath, resolve_factor_package,
    load_module_from_path, select_factor_columns, resolve_output_root, read_config)
from factor_protocol import resolve_data_root, resolve_factor_data_root
from factor_protocol.protocol import resolve_factor_columns
from factor_protocol.registry import load_core_factor_references
from .validation import require_validation
from .check import evaluation_config

DEFAULT_REFERENCE_FACTOR = "barra_cne5_styles"


def _resolve_reference_factors(
    reference_factors: Sequence[str] | None,
    *,
    project_root: Path,
    factor_zoo_root: str,
) -> tuple[str, ...]:
    if reference_factors:
        return tuple(
            dict.fromkeys(FactorPath.parse(value).label for value in reference_factors)
        )

    package = resolve_factor_package(
        DEFAULT_REFERENCE_FACTOR,
        project_root=project_root,
        factor_zoo_root=factor_zoo_root,
    )
    module = load_module_from_path(
        package.module_path,
        project_root=project_root,
        factor_name=DEFAULT_REFERENCE_FACTOR,
    )
    references = [
        FactorPath(DEFAULT_REFERENCE_FACTOR, column).label
        for column in resolve_factor_columns(module)
    ]
    references.extend(
        load_core_factor_references(
            project_root=project_root,
            factor_zoo_root=factor_zoo_root,
        )
    )
    return tuple(dict.fromkeys(references))


def run_package_evaluation(cfg):
    root = Path(cfg.project_root or Path.cwd())
    source = cfg.storage_factor_name or cfg.factor_name
    package = resolve_factor_package(cfg.factor_name, project_root=root,
        factor_module=cfg.factor_module, factor_zoo_root=cfg.factor_zoo_root)
    module = load_module_from_path(package.module_path, project_root=root, factor_name=cfg.factor_name)
    columns = select_factor_columns(module, cfg.factor_columns)
    end = cfg.end
    if not cfg.daily_update or end is None:
        factor_root = resolve_factor_data_root(project_root=root) / source
        end = max((path.name[5:] for path in factor_root.glob("date=????-??-??")
                   if path.is_dir() and (cfg.end is None or path.name[5:] <= cfg.end)),
                  default=None)
        if end is None:
            raise ValueError(f"factor data is unavailable: {factor_root}")
    output = resolve_output_root(cfg.output_dir, project_root=root) / cfg.factor_name
    for column in columns:
        require_validation(output / column, end=end)
    references = cfg.reference_factors if cfg.daily_update else _resolve_reference_factors(
        cfg.reference_factors, project_root=root, factor_zoo_root=cfg.factor_zoo_root
    )
    return run_evaluation(replace(cfg, factor_columns=columns, reference_factors=references))


def refresh_evaluations(
    *,
    factor_name: str,
    project_root: str | Path,
    data_root: str | Path | None = None,
    storage_factor_name: str | None = None,
    output_root: str | Path = "artifacts/factor_zoo",
    factor_zoo_root: str | Path | None = None,
    threads: int = 2,
    memory_limit: str | None = "4GB",
    temp_directory: str | Path = ".tmp/factor_evaluation",
    end: str | None = None,
    settings: EvaluationSettings | None = None,
    daily_parameters: Mapping[str, Any] | None = None,
) -> list[Path]:
    """Refresh a complete package, initializing missing evaluation records."""

    daily_update = daily_parameters is not None
    root = Path(project_root)
    settings = settings or EvaluationSettings.from_project_root(root)
    settings.validate()
    resolved_data_root = resolve_data_root(
        project_root=root,
        data_root=data_root,
    )
    factor_source = storage_factor_name or factor_name
    destination = resolve_output_root(output_root, project_root=root) / factor_name
    groups: dict[tuple[Any, ...], list[str]] = {}
    package = resolve_factor_package(
        factor_name,
        project_root=root,
        factor_zoo_root=factor_zoo_root,
    )
    module = load_module_from_path(
        package.module_path,
        project_root=root,
        factor_name=factor_name,
    )
    factor_columns = select_factor_columns(module, None)
    for column in factor_columns:
        column_dir = destination / column
        config = evaluation_config(read_config(column_dir / "config.json"))
        factor = config.get("factor")
        if isinstance(factor, Mapping) and (
            factor.get("name") != factor_name
            or factor.get("column") != column
            or config.get("input") != (factor_source if factor_source != factor_name else None)
        ):
            raise ValueError(
                f"evaluation config identifies another factor output: {column_dir}"
            )
        parameters = config.get("parameters") or {}
        if daily_update:
            if not config.get("files"):
                raise ValueError(f"daily update requires complete evaluation results: {column_dir}; run Evaluate")
            for key, value in daily_parameters.items():
                if parameters.get(key) != value:
                    raise ValueError(f"daily update cannot change evaluation parameter {key}: {column_dir}")
        references = tuple(
            str(value) for value in parameters.get("reference_factors", ())
        )
        key = (
            parameters.get("begin", settings.begin),
            str(parameters.get("train_end", settings.train_end)),
            str(parameters.get("validation_end", settings.validation_end)),
            int(parameters.get("horizon_days", settings.horizon_days)),
            int(parameters.get("execution_lag_days", settings.execution_lag_days)),
            str(parameters.get("price_col", settings.price_col)),
            int(parameters.get("quantiles", settings.quantiles)),
            float(parameters.get("top_percentile", settings.top_percentile)),
            int(parameters.get("rolling_mean_days", settings.rolling_mean_days)),
            str(
                parameters.get("industry_level", settings.neutralization_industry_level)
            ),
            references,
            str(parameters.get("direction", "auto")),
        )
        groups.setdefault(key, []).append(column)
    results: list[Path] = []
    for key, columns in groups.items():
        (
            begin,
            train_end,
            validation_end,
            horizon,
            lag,
            price,
            quantiles,
            top,
            rolling_mean_days,
            industry_level,
            references,
            direction,
        ) = key
        result = run_package_evaluation(
            EvaluationConfig(
                factor_name=factor_name,
                storage_factor_name=storage_factor_name,
                direction=direction,
                begin=str(begin),
                train_end=train_end,
                validation_end=validation_end,
                horizon_days=horizon,
                execution_lag_days=lag,
                price_col=price,
                quantiles=quantiles,
                top_percentile=top,
                rolling_mean_days=rolling_mean_days,
                neutralization_industry_level=industry_level,
                reference_factors=tuple(references),
                end=end,
                factor_columns=tuple(columns),
                data_root=resolved_data_root,
                output_dir=output_root,
                factor_zoo_root=factor_zoo_root,
                threads=threads,
                memory_limit=memory_limit,
                temp_directory=temp_directory,
                column_batch_size=settings.refresh_column_batch_size,
                date_batch_days=settings.refresh_date_batch_days,
                project_root=root,
                settings=settings,
                daily_update=daily_update,
            )
        )
        results.append(result)
    return results
