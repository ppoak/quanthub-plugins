"""Create a single descriptive overview figure from canonical evaluation CSVs.

This script intentionally lives with the factor-evaluate skill. It is not part
of ``factor-protocol`` and never changes canonical CSV or JSON artifacts.
"""

from __future__ import annotations

from .check import evaluation_files_match

import json
from pathlib import Path
from typing import Iterable

import matplotlib
from factor_protocol.artifacts import resolve_output_root

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.colors import Normalize
from matplotlib.figure import Figure
from matplotlib.gridspec import GridSpec

VARIANT_ORDER = ("raw", "neutralized", "rolling_mean")
VARIANT_COLORS = {
    "raw": "#1f77b4",
    "neutralized": "#ff7f0e",
    "rolling_mean": "#2ca02c",
}
REQUIRED_FILES = ("ic.csv", "returns.csv", "correlation.csv", "config.json")
REQUIRED_IC_COLUMNS = {"date", "variant", "ic"}
REQUIRED_RETURN_COLUMNS = {
    "date",
    "variant",
    "portfolio_id",
    "layer",
    "gross_return",
    "net_return_5bps",
    "gross_return_nav",
    "net_return_5bps_nav",
    "turnover",
    "top_gross_return_nav",
    "top_net_return_5bps_nav",
}
REQUIRED_CORRELATION_COLUMNS = {"date", "variant", "reference_factor", "correlation"}


def _parse_date(value: str | None, *, label: str) -> pd.Timestamp | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{label} must be an ISO date: {value}")
    return pd.Timestamp(parsed).normalize()


def _read_config(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read evaluation config: {path}") from exc


def _infer_test_window(
    config: dict,
    frames: Iterable[pd.DataFrame],
) -> tuple[pd.Timestamp, pd.Timestamp]:
    frames = tuple(frames)
    evaluation = config.get("evaluation", {})
    evaluation_begin = _parse_date(evaluation.get("begin"), label="evaluation begin")
    parameters = evaluation.get("parameters", {})
    begin_exclusive = _parse_date(parameters.get("validation_end"), label="test begin")
    begin = (
        begin_exclusive + pd.Timedelta(days=1) if begin_exclusive is not None else None
    )
    date_ends = [
        pd.Timestamp(frame["date"].max()).normalize()
        for frame in frames
        if not frame.empty
    ]
    end = min(date_ends) if date_ends else None

    dates = pd.concat((frame["date"] for frame in frames), ignore_index=True)
    if evaluation_begin is not None:
        begin = evaluation_begin
    elif begin is None:
        begin = pd.Timestamp(dates.min()).normalize()
    if end is None:
        end = pd.Timestamp(dates.max()).normalize()
    return begin, end


def _require_columns(frame: pd.DataFrame, required: set[str], name: str) -> None:
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"{name} is missing columns: {', '.join(missing)}")


def _load_inputs(
    column_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    missing = [name for name in REQUIRED_FILES if not (column_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"evaluation directory is incomplete: {column_dir}; missing {', '.join(missing)}"
        )
    ic = pd.read_csv(column_dir / "ic.csv")
    returns = pd.read_csv(column_dir / "returns.csv")
    correlation = pd.read_csv(column_dir / "correlation.csv")
    _require_columns(ic, REQUIRED_IC_COLUMNS, "ic.csv")
    _require_columns(returns, REQUIRED_RETURN_COLUMNS, "returns.csv")
    _require_columns(correlation, REQUIRED_CORRELATION_COLUMNS, "correlation.csv")
    config = _read_config(column_dir / "config.json")
    evaluation = config.get("evaluation", {})
    if evaluation.get("status") != "complete" or not evaluation_files_match(column_dir, evaluation):
        raise ValueError("overview requires complete matching metric files")
    for frame in (ic, returns, correlation):
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        frame["variant"] = frame["variant"].astype(str)
        frame.dropna(subset=["date"], inplace=True)
    for column in ("layer", "portfolio_id"):
        returns[column] = pd.to_numeric(returns[column], errors="coerce")
    return ic, returns, correlation, config


def _filter_window(
    frame: pd.DataFrame,
    begin: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    return frame.loc[(frame["date"] >= begin) & (frame["date"] <= end)].copy()


def _variant_values(frame: pd.DataFrame) -> list[str]:
    present = set(frame["variant"].dropna().unique())
    return [variant for variant in VARIANT_ORDER if variant in present] + sorted(
        present.difference(VARIANT_ORDER)
    )


def _plot_ic(ax: Axes, ic: pd.DataFrame) -> None:
    pivot = (
        ic.pivot_table(index="date", columns="variant", values="ic", aggfunc="mean")
        .sort_index()
        .reindex(columns=_variant_values(ic))
    )
    right = ax.twinx()
    handles: list[object] = []
    labels: list[str] = []
    daily_values: list[np.ndarray] = []
    cumulative_values: list[np.ndarray] = []
    for variant in pivot.columns:
        color = VARIANT_COLORS.get(variant, "#555555")
        daily = pd.to_numeric(pivot[variant], errors="coerce")
        cumulative = daily.fillna(0.0).cumsum()
        daily_values.append(daily.to_numpy(dtype=float))
        cumulative_values.append(cumulative.to_numpy(dtype=float))
        line = ax.plot(
            pivot.index,
            daily,
            color=color,
            linewidth=0.8,
            alpha=0.42,
            label=f"{variant} daily IC",
        )[0]
        cumulative_line = right.plot(
            pivot.index,
            cumulative,
            color=color,
            linewidth=1.7,
            label=f"{variant} cumulative IC sum",
        )[0]
        handles.extend([line, cumulative_line])
        labels.extend([line.get_label(), cumulative_line.get_label()])
    daily_finite = np.concatenate(
        [values[np.isfinite(values)] for values in daily_values]
    )
    cumulative_finite = np.concatenate(
        [values[np.isfinite(values)] for values in cumulative_values]
    )
    daily_limit = max(float(np.max(np.abs(daily_finite))) * 1.1, 0.01)
    cumulative_limit = max(float(np.max(np.abs(cumulative_finite))) * 1.1, 0.01)
    ax.set_ylim(-daily_limit, daily_limit)
    right.set_ylim(-cumulative_limit, cumulative_limit)
    ax.axhline(0.0, color="#888888", linewidth=0.7, linestyle=":")
    ax.set_title("Information coefficient: daily IC and cumulative sum")
    ax.set_ylabel("Daily Spearman IC")
    right.set_ylabel("Cumulative IC sum")
    ax.grid(axis="y", alpha=0.2)
    ax.legend(handles, labels, ncol=3, fontsize=8, loc="upper left")


def _plot_layer_boxplots(
    axes: Iterable[Axes],
    returns: pd.DataFrame,
    return_column: str,
) -> None:
    means = (
        returns.groupby(["variant", "layer", "portfolio_id"], observed=True)[
            return_column
        ]
        .mean()
        .reset_index()
    )
    layers = sorted(pd.to_numeric(means["layer"], errors="coerce").dropna().unique())
    for ax, variant in zip(axes, _variant_values(means)):
        variant_frame = means.loc[means["variant"] == variant]
        data = [
            variant_frame.loc[variant_frame["layer"] == layer, return_column]
            .dropna()
            .to_numpy()
            for layer in layers
        ]
        data = [values if len(values) else np.asarray([np.nan]) for values in data]
        artists = ax.boxplot(
            data,
            positions=np.arange(1, len(layers) + 1),
            widths=0.65,
            patch_artist=True,
            tick_labels=[f"L{int(layer)}" for layer in layers],
            showfliers=True,
            manage_ticks=False,
        )
        for box in artists["boxes"]:
            box.set_facecolor(VARIANT_COLORS.get(variant, "#777777"))
            box.set_alpha(0.35)
            box.set_edgecolor(VARIANT_COLORS.get(variant, "#777777"))
        for line in artists["medians"]:
            line.set_color("#222222")
            line.set_linewidth(1.2)
        for layer_number, values in enumerate(data, start=1):
            finite = values[np.isfinite(values)]
            if len(finite):
                jitter = np.linspace(-0.12, 0.12, len(finite))
                ax.scatter(
                    np.full(len(finite), layer_number) + jitter,
                    finite,
                    color=VARIANT_COLORS.get(variant, "#777777"),
                    s=12,
                    alpha=0.75,
                    zorder=3,
                )
        ax.axhline(0.0, color="#888888", linewidth=0.7, linestyle=":")
        ax.set_title(variant)
        ax.set_xlabel("Layer")
        ax.grid(axis="y", alpha=0.2)
    first = next(iter(axes), None)
    if first is not None:
        first.set_ylabel(f"Portfolio-id mean {return_column}")


def _align_portfolio_sequences(
    returns: pd.DataFrame,
    begin: pd.Timestamp,
    end: pd.Timestamp,
    horizon_days: int,
) -> pd.DataFrame:
    """Align staggered portfolio paths by their own observation sequence step.

    ``portfolio_id`` is a staggered return sequence, so an original signal date
    contains only one portfolio id.  The plotting axis must therefore compare
    the first observation of every sequence with the first observation of the
    others, the second with the second, and so on.  Each sequence step occupies
    one evaluation horizon on the synthetic calendar.  Using the global date
    position of each sparse observation here would make the aligned date depend
    on holiday or missing-row gaps and would produce dates with one, three, or
    five portfolio ids.
    """

    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    frame = returns.dropna(subset=["date", "variant", "portfolio_id"]).copy()
    if frame.empty:
        return frame.assign(aligned_date=pd.NaT)

    # Returns contain one row per layer.  Build the sequence index on unique
    # (variant, portfolio_id, date) observations, then merge it back so every
    # layer on the same observation receives the same aligned date.
    observations = (
        frame[["date", "variant", "portfolio_id"]]
        .drop_duplicates()
        .sort_values(["variant", "portfolio_id", "date"])
        .reset_index(drop=True)
    )
    observations["_sequence_step"] = observations.groupby(
        ["variant", "portfolio_id"], observed=True
    ).cumcount()

    calendar = pd.Index(sorted(frame["date"].unique()))
    positions = pd.Series(np.arange(len(calendar), dtype=int), index=calendar)
    observations["_date_position"] = observations["date"].map(positions).astype("Int64")
    variant_start = observations.groupby("variant", observed=True)[
        "_date_position"
    ].min()
    observations["_aligned_position"] = observations["variant"].map(variant_start) + (
        observations["_sequence_step"] * horizon_days
    )
    valid = observations["_aligned_position"].between(0, len(calendar) - 1)
    observations = observations.loc[valid].copy()
    observations["aligned_date"] = pd.to_datetime(
        [calendar[int(position)] for position in observations["_aligned_position"]]
    )

    frame = frame.merge(
        observations[["date", "variant", "portfolio_id", "aligned_date"]],
        on=["date", "variant", "portfolio_id"],
        how="inner",
        validate="many_to_one",
    )
    frame = frame.loc[
        (frame["aligned_date"] >= begin) & (frame["aligned_date"] <= end)
    ].copy()
    return frame


def _plot_layer_nav(axes: Iterable[Axes], returns: pd.DataFrame) -> None:
    grouped = (
        returns.groupby(["aligned_date", "variant", "layer"], observed=True)[
            "net_return_5bps_nav"
        ]
        .mean()
        .reset_index()
    )
    layers = sorted(pd.to_numeric(grouped["layer"], errors="coerce").dropna().unique())
    layer_colors = plt.cm.viridis(np.linspace(0.08, 0.92, max(len(layers), 1)))
    for ax, variant in zip(axes, _variant_values(grouped)):
        variant_frame = grouped.loc[grouped["variant"] == variant]
        for layer, color in zip(layers, layer_colors):
            series = variant_frame.loc[variant_frame["layer"] == layer].sort_values(
                "aligned_date"
            )
            if series.empty:
                continue
            ax.plot(
                series["aligned_date"],
                pd.to_numeric(series["net_return_5bps_nav"], errors="coerce"),
                color=color,
                linewidth=0.9,
                label=f"L{int(layer)}",
            )
        ax.set_title(f"{variant}: aligned layer net NAV")
        ax.set_ylabel("NAV")
        ax.grid(alpha=0.2)
    first = next(iter(axes), None)
    if first is not None:
        first.legend(ncol=5, fontsize=7, loc="upper left")


def _plot_turnover(
    axes: Iterable[Axes], returns: pd.DataFrame, turnover_window: int
) -> None:
    grouped = (
        returns.groupby(["aligned_date", "variant", "layer"], observed=True)["turnover"]
        .mean()
        .reset_index()
    )
    layers = sorted(pd.to_numeric(grouped["layer"], errors="coerce").dropna().unique())
    layer_colors = plt.cm.viridis(np.linspace(0.08, 0.92, max(len(layers), 1)))
    for ax, variant in zip(axes, _variant_values(grouped)):
        variant_frame = grouped.loc[grouped["variant"] == variant]
        for layer, color in zip(layers, layer_colors):
            series = variant_frame.loc[variant_frame["layer"] == layer].sort_values(
                "aligned_date"
            )
            if series.empty:
                continue
            turnover = pd.to_numeric(series["turnover"], errors="coerce")
            smoothed = turnover.rolling(turnover_window, min_periods=1).mean()
            ax.plot(
                series["aligned_date"],
                smoothed,
                color=color,
                linewidth=0.9,
                label=f"L{int(layer)}",
            )
        ax.set_title(f"{variant}: {turnover_window}D mean aligned layer turnover")
        ax.set_ylabel("Turnover")
        ax.set_ylim(bottom=0.0)
        ax.grid(alpha=0.2)
    first = next(iter(axes), None)
    if first is not None:
        first.legend(ncol=5, fontsize=7, loc="upper left")


def _plot_top_nav(axes: Iterable[Axes], returns: pd.DataFrame) -> None:
    top = returns.drop_duplicates(["aligned_date", "variant", "portfolio_id"])
    grouped = (
        top.groupby(["aligned_date", "variant"], observed=True)[
            ["top_gross_return_nav", "top_net_return_5bps_nav"]
        ]
        .mean()
        .reset_index()
    )
    for ax, variant in zip(axes, _variant_values(grouped)):
        series = grouped.loc[grouped["variant"] == variant].sort_values("aligned_date")
        color = VARIANT_COLORS.get(variant, "#555555")
        ax.plot(
            series["aligned_date"],
            pd.to_numeric(series["top_gross_return_nav"], errors="coerce"),
            color=color,
            linewidth=1.4,
            label="gross",
        )
        ax.plot(
            series["aligned_date"],
            pd.to_numeric(series["top_net_return_5bps_nav"], errors="coerce"),
            color=color,
            linewidth=1.2,
            linestyle="--",
            label="net (5 bps)",
        )
        ax.set_title(f"{variant}: aligned Top gross/net NAV mean")
        ax.set_ylabel("NAV")
        ax.grid(alpha=0.2)
        ax.legend(fontsize=8, loc="upper left")


def _plot_correlation(ax: Axes, correlation: pd.DataFrame) -> None:
    grouped = (
        correlation.groupby(["variant", "reference_factor"], observed=True)[
            "correlation"
        ]
        .mean()
        .reset_index()
    )
    variants = _variant_values(grouped)
    references = sorted(grouped["reference_factor"].dropna().unique())
    matrix = grouped.pivot(
        index="variant", columns="reference_factor", values="correlation"
    ).reindex(index=variants, columns=references)
    image = ax.imshow(
        matrix.to_numpy(dtype=float),
        aspect="auto",
        cmap="coolwarm",
        norm=Normalize(vmin=-1.0, vmax=1.0),
    )
    ax.set_yticks(np.arange(len(variants)), labels=variants)
    short_references = [reference.split(".")[-1] for reference in references]
    ax.set_xticks(
        np.arange(len(references)), labels=short_references, rotation=45, ha="right"
    )
    for row in range(matrix.shape[0]):
        for column in range(matrix.shape[1]):
            value = matrix.iloc[row, column]
            if pd.notna(value):
                ax.text(
                    column, row, f"{value:.2f}", ha="center", va="center", fontsize=8
                )
    ax.set_title("Mean daily Spearman correlation with reference factors")
    ax.set_xlabel("Reference factor")
    ax.set_ylabel("Variant")
    ax.figure.colorbar(image, ax=ax, fraction=0.02, pad=0.01, label="Mean correlation")


def build_figure(
    *,
    column: str,
    ic: pd.DataFrame,
    returns: pd.DataFrame,
    correlation: pd.DataFrame,
    begin: pd.Timestamp,
    end: pd.Timestamp,
    turnover_window: int,
    return_column: str,
    horizon_days: int = 5,
) -> Figure:
    if turnover_window <= 0:
        raise ValueError("turnover_window must be positive")
    if return_column not in {"gross_return", "net_return_5bps"}:
        raise ValueError("return_column must be gross_return or net_return_5bps")

    filtered_ic = _filter_window(ic, begin, end)
    filtered_returns = _filter_window(returns, begin, end)
    filtered_correlation = _filter_window(correlation, begin, end)
    aligned_returns = _align_portfolio_sequences(returns, begin, end, horizon_days)
    if (
        filtered_ic.empty
        or filtered_returns.empty
        or aligned_returns.empty
        or filtered_correlation.empty
    ):
        raise ValueError("the selected window has incomplete evaluation data")

    figure = plt.figure(figsize=(24, 32), facecolor="white")
    grid = GridSpec(
        6,
        1,
        figure=figure,
        height_ratios=[1.3, 1.45, 1.6, 1.35, 1.4, 1.35],
        hspace=0.48,
        top=0.96,
        bottom=0.035,
        left=0.045,
        right=0.965,
    )

    ic_axis = figure.add_subplot(grid[0, 0])
    _plot_ic(ic_axis, filtered_ic)

    box_grid = grid[1, 0].subgridspec(1, 3, wspace=0.22)
    box_axes = [figure.add_subplot(box_grid[0, index]) for index in range(3)]
    _plot_layer_boxplots(box_axes, filtered_returns, return_column)

    nav_grid = grid[2, 0].subgridspec(1, 3, wspace=0.2)
    nav_axes = [figure.add_subplot(nav_grid[0, index]) for index in range(3)]
    _plot_layer_nav(nav_axes, aligned_returns)

    turnover_grid = grid[3, 0].subgridspec(1, 3, wspace=0.2)
    turnover_axes = [figure.add_subplot(turnover_grid[0, index]) for index in range(3)]
    _plot_turnover(turnover_axes, aligned_returns, turnover_window)

    top_grid = grid[4, 0].subgridspec(1, 3, wspace=0.2)
    top_axes = [figure.add_subplot(top_grid[0, index]) for index in range(3)]
    _plot_top_nav(top_axes, aligned_returns)

    correlation_axis = figure.add_subplot(grid[5, 0])
    _plot_correlation(correlation_axis, filtered_correlation)

    figure.suptitle(
        f"Evaluation overview: {column}\n{begin.date().isoformat()} to {end.date().isoformat()}",
        fontsize=20,
        fontweight="bold",
        y=0.995,
    )
    return figure


def _factor_column(
    output_dir: Path, factor_name: str | None, requested: str | None
) -> tuple[str, Path]:
    root = output_dir / factor_name if factor_name is not None else output_dir
    if requested:
        return requested, root / requested
    columns = sorted(path.name for path in root.iterdir() if path.is_dir())
    if len(columns) != 1:
        raise ValueError(
            "--factor-column is required when evaluation output contains multiple columns: "
            + ", ".join(columns)
        )
    return columns[0], root / columns[0]


def render(*, project_root, factor_name=None, factor_column=None, output_dir="artifacts/factor_zoo",
           test_begin=None, test_end=None, turnover_window=20,
           return_column="net_return_5bps", output=None, dpi=180):
    """Render one complete column without modifying metric files."""
    project_root = Path(project_root).resolve()
    output_dir = resolve_output_root(output_dir, project_root=project_root)
    column, column_dir = _factor_column(
        output_dir, factor_name, factor_column
    )
    ic, returns, correlation, config = _load_inputs(column_dir)
    inferred_begin, inferred_end = _infer_test_window(
        config, (ic, returns, correlation)
    )
    begin = _parse_date(test_begin, label="test-begin") or inferred_begin
    end = _parse_date(test_end, label="test-end") or inferred_end
    if begin > end:
        raise ValueError("test-begin must not be later than test-end")
    horizon_days = int(
        config.get("evaluation", {}).get("parameters", {}).get("horizon_days", 5)
    )
    figure = build_figure(
        column=column,
        ic=ic,
        returns=returns,
        correlation=correlation,
        begin=begin,
        end=end,
        turnover_window=int(turnover_window),
        return_column=return_column,
        horizon_days=horizon_days,
    )
    output = (
        resolve_output_root(output, project_root=project_root)
        if output
        else column_dir / "overview.png"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=int(dpi), bbox_inches="tight")
    plt.close(figure)
    print(
        f"wrote evaluation overview: column={column} "
        f"begin={begin.date().isoformat()} end={end.date().isoformat()} path={output}",
        flush=True,
    )
    return output
