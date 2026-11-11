"""Evaluate factor packages or explicit factor data through one configurable pipeline."""
from argparse import ArgumentParser, Namespace
from pathlib import Path
from core.shared import EvaluationSettings
from core.evaluation import run


def add_arguments(parser: ArgumentParser) -> None:
    """Expose the canonical DuckDB evaluation command."""

    parser.add_argument("--project-root", default=".")
    parser.add_argument("--task", action="append", choices=("ic", "portfolio", "correlation"))
    mode = parser.add_mutually_exclusive_group()

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--all-alpha",
        action="store_true",
        help="Evaluate every non-Barra alpha factor.",
    )
    source.add_argument("--factor-package", action="append", default=[])
    source.add_argument("--factor-data", metavar="DIRECTORY", help="Read scores from this Hive Parquet directory.")
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh existing canonical evaluation artifacts for one factor.",
    )
    parser.add_argument("--data-root")
    parser.add_argument("--factor-zoo-root")
    parser.add_argument("--storage-factor-name")
    parser.add_argument("--factor-column", action="append", default=None)
    parser.add_argument("--begin", default=None)
    parser.add_argument("--train-end", default=None)
    parser.add_argument("--validation-end", default=None)
    parser.add_argument("--direction", type=str.lower, choices=("positive", "negative", "auto"), default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--horizon-days", type=int, default=None)
    parser.add_argument("--execution-lag-days", type=int, default=None)
    parser.add_argument("--price-col", default=None)
    parser.add_argument("--quantiles", type=int, default=None)
    parser.add_argument("--top-percentile", type=float, default=None)
    parser.add_argument("--rolling-mean-days", type=int, default=None)
    parser.add_argument(
        "--neutralization-industry-level",
        choices=("first_industry_code", "second_industry_code", "third_industry_code"),
        default=None,
    )
    parser.add_argument("--reference-factor", action="append", default=None)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--memory-limit", default=None)
    parser.add_argument("--temp-directory", default=None)
    parser.add_argument("--output-dir", help="Evaluation output directory.")
    parser.add_argument("--column-batch-size", type=int, default=None)
    parser.add_argument("--date-batch-days", type=int, default=None)
    parser.add_argument(
        "--intra-column-workers",
        type=int,
        default=None,
        help=(
            "Split one full-rebuild column into isolated SQL metric and "
            "reference-correlation workers. Memory is divided across them."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "Run factor-column tasks in isolated worker processes. Threads and "
            "memory limit apply to each worker."
        ),
    )
    parser.add_argument(
        "--worker-column-group-size",
        type=int,
        default=None,
        help=(
            "Keep up to this many columns from one source in one worker task. "
            "The default of 1 bounds memory per logical column."
        ),
    )
    mode.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild canonical evaluation artifacts from the requested begin date.",
    )
    parser.add_argument("--operation", action="append", choices=("evaluate", "check", "plot", "readme"),
        help="Select pipeline operations; default: evaluate. Repeat to combine operations.")


def _apply_settings(args: Namespace, settings: EvaluationSettings) -> None:
    defaults = {
        "direction": settings.direction,
        "begin": settings.begin,
        "train_end": settings.train_end,
        "validation_end": settings.validation_end,
        "horizon_days": settings.horizon_days,
        "execution_lag_days": settings.execution_lag_days,
        "price_col": settings.price_col,
        "quantiles": settings.quantiles,
        "top_percentile": settings.top_percentile,
        "rolling_mean_days": settings.rolling_mean_days,
        "neutralization_industry_level": settings.neutralization_industry_level,
        "threads": settings.threads,
        "memory_limit": settings.memory_limit,
        "temp_directory": settings.temp_directory,
        "workers": settings.workers,
        "worker_column_group_size": settings.worker_column_group_size,
        "intra_column_workers": settings.intra_column_workers,
        "column_batch_size": settings.column_batch_size,
        "date_batch_days": settings.date_batch_days,
    }
    for name, value in defaults.items():
        if getattr(args, name) is None:
            setattr(args, name, value)
    args.settings = settings


def main():
    parser = ArgumentParser(description=__doc__)
    add_arguments(parser)
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    _apply_settings(args, EvaluationSettings.from_project_root(root))
    try:
        return run(args, project_root=root)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Evaluation failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
