"""Common broker evaluation helpers.

The evaluator treats executions as the canonical historical facts. It rebuilds
cash, positions, NAV, market value, book value, and trade statistics from a
stored execution stream plus an external price matrix.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import time as datetime_time
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

@dataclass(frozen=True)
class Trade:
    """Round-trip trade derived from executions by accounting rules."""

    code: str
    open_execution_id: str | None
    open_timestamp: Any
    open_price: float
    open_fee: float
    quantity: float
    close_execution_id: str | None
    close_timestamp: Any
    close_price: float
    close_fee: float
    realized_pnl: float

    @property
    def duration_days(self) -> int | None:
        """Calendar days from open to close."""
        if not (self.open_timestamp and self.close_timestamp):
            return None
        return (pd.to_datetime(self.close_timestamp) - pd.to_datetime(self.open_timestamp)).days

    @property
    def return_pct(self) -> float | None:
        """Return on matched cost basis after fees."""
        cost = self.open_price * self.quantity + self.open_fee + self.close_fee
        return None if cost == 0 else self.realized_pnl / cost

    @property
    def is_winning(self) -> bool:
        """Whether this round-trip trade was profitable."""
        return self.realized_pnl > 0

    def to_dict(self) -> dict[str, Any]:
        """Return fields as a plain dict."""
        return asdict(self)

def evaluate_executions(
    executions: Iterable[Any],
    *,
    prices: pd.DataFrame,
    initial_cash: float = 0.0,
    initial_positions: Mapping[str, float] | None = None,
    benchmark: pd.Series | None = None,
) -> pd.Series:
    """Rebuild cash, positions, and performance from executions and prices.

    External cash flows are assigned to the first valuation at or after their
    timestamp. A daily date-only index treats intraday flows before 15:00 as
    part of that date and later flows as part of the next valuation.
    """

    if prices.empty:
        return evaluate_asset_value(pd.DataFrame(), benchmark=benchmark)

    index = pd.DatetimeIndex(pd.to_datetime(prices.index), name="datetime")
    price_matrix = prices.copy()
    price_matrix.index = index
    codes = sorted(set(price_matrix.columns) | set(initial_positions or {}))
    shares = pd.DataFrame(0.0, index=index, columns=codes)
    cash_delta = pd.Series(0.0, index=index)
    external_flow = pd.Series(0.0, index=index)
    late_flow = pd.Series(0.0, index=index)

    for code, quantity in (initial_positions or {}).items():
        if code in shares:
            shares.loc[:, code] = float(quantity)

    timed = [(_align_timestamp(_execution_timestamp(item), index.tz), item) for item in executions]
    for timestamp, execution in sorted(timed, key=lambda item: item[0]):
        if pd.isna(timestamp):
            continue
        location = _flow_location(timestamp, index)
        if location is None:
            continue
        code = _execution_code(execution)
        side = str(_execution_side(execution) or "").upper()
        quantity = float(_execution_quantity(execution) or 0.0)
        price = float(_execution_price(execution) or 0.0)
        comm = float(_execution_comm(execution) or 0.0)
        if side in {"TRANSFER", "DEPOSIT", "WITHDRAW", "WITHDRAWAL"}:
            sign = -1 if side in {"WITHDRAW", "WITHDRAWAL"} else 1
            raw_cashflow = getattr(execution, "cashflow", sign * quantity * price)
            cashflow = float(raw_cashflow)
            cash_delta.iloc[location:] += cashflow
            external_flow.iloc[location] += cashflow
            same_daily_date = index[location].date() == timestamp.date() and index[location].time() == datetime_time(0, 0)
            between_valuations = location > 0 and index[location - 1] < timestamp < index[location]
            if between_valuations and not same_daily_date:
                late_flow.iloc[location] += cashflow
            continue
        if code and code != "CASH" and code not in shares:
            shares[code] = 0.0
            price_matrix[code] = np.nan
        if not code or code == "CASH" or code not in shares:
            continue
        raw_cashflow = getattr(execution, "cashflow", None)
        if raw_cashflow is None:
            raw_cashflow = -(quantity * price + comm) if side == "BUY" else quantity * price - comm
        cashflow = float(raw_cashflow)
        if side == "BUY":
            shares.iloc[location:, shares.columns.get_loc(code)] += quantity
            cash_delta.iloc[location:] += cashflow
        elif side == "SELL":
            shares.iloc[location:, shares.columns.get_loc(code)] -= quantity
            cash_delta.iloc[location:] += cashflow

    marks = price_matrix.reindex(columns=shares.columns).apply(pd.to_numeric, errors="coerce").astype(float)
    marks = marks.where(np.isfinite(marks) & (marks > 0))
    position_values = (shares * marks).where(shares.ne(0), 0.0)
    market_value = position_values.sum(axis=1, skipna=False)
    cash = float(initial_cash) + cash_delta
    initial_nav = float(initial_cash)
    if not shares.empty:
        first_marks = marks.iloc[0]
        for code, quantity in (initial_positions or {}).items():
            mark = first_marks.get(code, np.nan)
            if pd.notna(mark) and np.isfinite(mark):
                initial_nav += float(quantity) * float(mark)
            else:
                initial_nav = np.nan
                break

    frame = pd.DataFrame(
        {
            "cash": cash,
            "book_value": np.nan,
            "market_value": market_value,
            "nav": cash + market_value,
            "external_flow": external_flow,
            "late_flow": late_flow,
            "initial_nav": initial_nav,
        },
        index=index,
    )
    return evaluate_asset_value(frame, benchmark=benchmark)


def evaluate_asset_value(
    asset_value: pd.DataFrame,
    *,
    benchmark: pd.Series | None = None,
) -> pd.Series:
    """Compute performance with timed external flows and missing-value gaps."""

    metrics = _empty_metrics()
    if asset_value.empty or "nav" not in asset_value:
        return pd.Series(metrics, name="metrics")

    actual_nav = pd.to_numeric(asset_value["nav"], errors="coerce").astype(float)
    final_nav = float(actual_nav.iloc[-1]) if pd.notna(actual_nav.iloc[-1]) else float("nan")
    flow = (
        pd.to_numeric(asset_value["external_flow"], errors="coerce").fillna(0.0)
        if "external_flow" in asset_value
        else pd.Series(0.0, index=asset_value.index)
    )
    late_flow = (
        pd.to_numeric(asset_value["late_flow"], errors="coerce").fillna(0.0)
        if "late_flow" in asset_value
        else pd.Series(0.0, index=asset_value.index)
    )
    valid_locations = np.flatnonzero(actual_nav.notna().to_numpy())
    if not len(valid_locations):
        metrics["final_nav"] = final_nav
        return pd.Series(metrics, name="metrics")

    first_location = int(valid_locations[0])
    baseline = (
        float(asset_value["initial_nav"].iloc[first_location])
        if "initial_nav" in asset_value and pd.notna(asset_value["initial_nav"].iloc[first_location])
        else float(actual_nav.iloc[first_location])
    )
    has_virtual_start = "initial_nav" in asset_value and pd.notna(asset_value["initial_nav"].iloc[first_location])
    period_returns: list[float] = []
    period_index: list[Any] = []
    previous_location = first_location
    previous_nav = float(actual_nav.iloc[first_location])
    if has_virtual_start:
        first_flow = float(flow.iloc[: first_location + 1].sum())
        first_late_flow = float(late_flow.iloc[: first_location + 1].sum())
        capital = baseline + first_flow - first_late_flow
        if capital > 0:
            period_returns.append((previous_nav - first_late_flow - capital) / capital)
            period_index.append(actual_nav.index[first_location])

    stop_location = len(valid_locations)
    for current_position in range(1, len(valid_locations)):
        current_location = int(valid_locations[current_position])
        gap = actual_nav.iloc[previous_location + 1 : current_location].isna().any()
        interval_flow = float(flow.iloc[previous_location + 1 : current_location + 1].sum())
        current_late_flow = float(late_flow.iloc[current_location])
        if gap and abs(interval_flow) > 0:
            stop_location = current_position
            break
        capital = previous_nav + interval_flow - current_late_flow
        current_nav = float(actual_nav.iloc[current_location])
        if capital > 0:
            period_returns.append((current_nav - current_late_flow - capital) / capital)
            period_index.append(actual_nav.index[current_location])
        previous_location = current_location
        previous_nav = current_nav

    returns = pd.Series(period_returns, index=pd.Index(period_index, name=actual_nav.index.name), dtype=float)
    init_cash = baseline
    growth = float((1.0 + returns).prod()) if len(returns) else 1.0
    total_return = growth - 1.0
    performance_index = returns.index
    if len(returns):
        wealth = pd.Series((1.0 + returns).cumprod().to_numpy(), index=performance_index)
        performance_nav = wealth * (baseline if baseline > 0 else 1.0)
    else:
        performance_nav = pd.Series(dtype=float)
    years = _years_elapsed(performance_index) if len(performance_index) > 1 else None
    annualized_return = growth ** (1.0 / years) - 1.0 if years and growth > 0 else None
    periods_per_year = max(1.0, (len(returns) - 1) / years) if years and years > 0 else 252
    ann_factor = math.sqrt(periods_per_year)
    volatility_annual = float(returns.std(ddof=1) * ann_factor) if len(returns) > 1 else 0.0
    sharpe_ratio = annualized_return / volatility_annual if annualized_return is not None and volatility_annual else None
    downside = returns[returns < 0]
    downside_std = float(downside.std(ddof=1) * ann_factor) if len(downside) > 1 else 0.0
    sortino_ratio = annualized_return / downside_std if annualized_return is not None and downside_std else None

    running_max = performance_nav.cummax()
    drawdown = performance_nav - running_max
    pct_drawdown = drawdown / running_max
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0
    max_dd_pct = float(pct_drawdown.min()) if len(pct_drawdown) else 0.0
    max_dd_date = pct_drawdown.idxmin() if len(pct_drawdown) else None
    calmar_ratio = annualized_return / abs(max_dd_pct) if annualized_return is not None and max_dd_pct else None
    win_rate = float((returns > 0).sum()) / len(returns) if len(returns) else None
    gross_profit = float(returns[returns > 0].sum())
    gross_loss = abs(float(returns[returns < 0].sum()))
    profit_factor = gross_profit / gross_loss if gross_loss else None

    net_base = init_cash if init_cash > 0 else float(performance_nav.abs().max()) if len(performance_nav) else 0.0
    net = performance_nav / net_base if net_base else performance_nav * 0.0
    bm_returns = _benchmark_returns(benchmark, returns.index)
    benchmark_values = bm_returns.to_numpy()
    if len(benchmark_values) < len(returns):
        benchmark_values = np.pad(benchmark_values, (len(returns) - len(benchmark_values), 0))
    excess_returns = returns.to_numpy() - benchmark_values[: len(returns)]
    tracking_error = float(np.std(excess_returns) * ann_factor) if len(excess_returns) else None
    var_5 = float(np.percentile(returns.to_numpy(), 5)) if len(returns) else None
    cvar_5 = float(returns[returns <= var_5].mean()) if var_5 is not None else None
    beta = float(returns.cov(bm_returns) / bm_returns.var()) if len(returns) > 30 and bm_returns.var() else None
    alpha = float((returns.mean() - beta * bm_returns.mean()) * periods_per_year) if beta is not None else None
    monthly_returns = net.resample("ME").last().pct_change(fill_method=None).fillna(0) if len(net) else pd.Series(dtype=float)

    metrics.update(
        {
            "init_cash": init_cash,
            "final_nav": final_nav,
            "total_return": total_return,
            "annualized_return": annualized_return,
            "volatility_annual": volatility_annual,
            "sharpe_ratio": sharpe_ratio,
            "sortino_ratio": sortino_ratio,
            "max_drawdown": max_dd,
            "max_drawdown_pct": max_dd_pct,
            "max_drawdown_date": str(max_dd_date) if max_dd_date is not None else None,
            "calmar_ratio": calmar_ratio,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "max_drawdown_period": _drawdown_period(drawdown, pct_drawdown) if len(drawdown) else None,
            "VaR_5%": var_5,
            "CVaR_5%": cvar_5,
            "beta": beta,
            "alpha": alpha,
            "excess_return": float(pd.Series(1 + excess_returns).cumprod().iloc[-1] - 1) if len(excess_returns) else None,
            "excess_volatility": tracking_error,
            "information_ratio": None,
            "skewness": float(returns.skew()) if len(returns) > 2 else None,
            "kurtosis": float(returns.kurtosis()) if len(returns) > 3 else None,
            "day_return_win_rate": win_rate,
            "monthly_return_std": float(monthly_returns.std(ddof=1)) if len(monthly_returns) > 1 else None,
            "monthly_win_rate": float((monthly_returns > 0).sum()) / len(monthly_returns) if len(monthly_returns) else None,
        }
    )
    if metrics["excess_return"] is not None and tracking_error:
        metrics["information_ratio"] = metrics["excess_return"] / tracking_error
    return pd.Series(metrics, name="metrics")


def _empty_metrics() -> dict[str, Any]:
    return {
        "init_cash": None,
        "final_nav": None,
        "total_return": None,
        "annualized_return": None,
        "volatility_annual": None,
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "max_drawdown": None,
        "max_drawdown_pct": None,
        "max_drawdown_date": None,
        "calmar_ratio": None,
        "win_rate": None,
        "profit_factor": None,
        "total_orders": 0,
        "total_trades": 0,
        "total_commission": 0.0,
        "max_drawdown_period": None,
        "VaR_5%": None,
        "CVaR_5%": None,
        "beta": None,
        "alpha": None,
        "excess_return": None,
        "excess_volatility": None,
        "information_ratio": None,
        "skewness": None,
        "kurtosis": None,
        "day_return_win_rate": None,
        "monthly_return_std": None,
        "monthly_win_rate": None,
        "trade_count": 0,
        "trade_win_rate": None,
        "avg_position_duration": None,
        "avg_return": None,
    }


def _benchmark_returns(benchmark: pd.Series | None, index: pd.Index) -> pd.Series:
    if benchmark is None or benchmark.empty:
        return pd.Series(np.zeros(len(index) - 1), index=index[1:])
    bm = benchmark.reindex(index).ffill().fillna(1.0)
    return bm.pct_change(fill_method=None).fillna(0).iloc[1:]


def _years_elapsed(index: pd.Index) -> float | None:
    if len(index) < 2:
        return None
    first = index[0]
    last = index[-1]
    if isinstance(first, pd.Timestamp) and isinstance(last, pd.Timestamp):
        days = (last - first).days
        return days / 365.0 if days > 0 else None
    return None


def _drawdown_period(drawdown: pd.Series, pct_drawdown: pd.Series) -> int | None:
    if pct_drawdown.empty:
        return None
    end_label = pct_drawdown.idxmin()
    end_loc = pct_drawdown.index.get_loc(end_label)
    before_end = drawdown.loc[:end_label]
    zero_mask = (before_end == 0).values
    if not zero_mask.any():
        return 0
    start_loc = end_loc - int(np.argmax(zero_mask[::-1]))
    return int(end_loc - start_loc)


def _align_timestamp(value: Any, timezone) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(timezone) if timezone is not None else timestamp
    if timezone is None:
        return timestamp.tz_localize(None)
    return timestamp.tz_convert(timezone)


def _flow_location(timestamp: pd.Timestamp, index: pd.DatetimeIndex) -> int | None:
    """Map a cash-flow timestamp to the valuation that first includes it."""
    location = int(index.searchsorted(timestamp, side="left"))
    if location >= len(index):
        return None
    if location > 0:
        previous = index[location - 1]
        same_date = previous.date() == timestamp.date()
        daily_bar = previous.time() == datetime_time(0, 0)
        before_close = timestamp.time() < datetime_time(15, 0)
        if same_date and daily_bar and before_close:
            return location - 1
    return location


def _execution_timestamp(execution: Any) -> Any:
    return _first_attr(execution, "timestamp", "datetime")


def _execution_code(execution: Any) -> str:
    return str(_first_attr(execution, "code") or "")


def _execution_side(execution: Any) -> Any:
    return _first_attr(execution, "side")


def _execution_quantity(execution: Any) -> Any:
    return _first_attr(execution, "quantity")


def _execution_price(execution: Any) -> Any:
    return _first_attr(execution, "price")


def _execution_comm(execution: Any) -> Any:
    return _first_attr(execution, "comm")


def _first_attr(value: Any, *names: str) -> Any:
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None
