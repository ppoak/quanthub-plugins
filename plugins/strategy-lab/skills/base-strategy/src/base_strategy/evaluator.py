"""Common broker evaluation helpers.

The evaluator treats executions as the canonical historical facts. It rebuilds
cash, positions, NAV, market value, book value, and trade statistics from a
stored execution stream plus an external price matrix.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
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
    """Reconstruct holdings from executions and evaluate the resulting NAV path.

    ``prices`` must be indexed by bar timestamp and have instrument codes as
    columns. Execution timestamps are aligned to the latest bar at or before
    the execution timestamp.
    """

    if prices.empty:
        return evaluate_asset_value(pd.DataFrame(), benchmark=benchmark)

    index = pd.Index(pd.to_datetime(prices.index), name="datetime")
    price_matrix = prices.copy()
    price_matrix.index = index
    codes = sorted(set(price_matrix.columns) | set(initial_positions or {}))
    shares = pd.DataFrame(0.0, index=index, columns=codes)
    cash_delta = pd.Series(0.0, index=index)

    for code, quantity in (initial_positions or {}).items():
        if code in shares:
            shares.loc[:, code] = float(quantity)

    for execution in sorted(executions, key=lambda item: pd.Timestamp(_execution_timestamp(item))):
        ts = pd.Timestamp(_execution_timestamp(execution))
        loc = index.searchsorted(ts, side="right") - 1
        if loc < 0:
            continue
        code = _execution_code(execution)
        side = str(_execution_side(execution) or "").upper()
        quantity = float(_execution_quantity(execution) or 0.0)
        price = float(_execution_price(execution) or 0.0)
        comm = float(_execution_comm(execution) or 0.0)
        if side in {"TRANSFER", "DEPOSIT"}:
            cash_delta.iloc[loc:] += quantity * price
            continue
        if side in {"WITHDRAW", "WITHDRAWAL"}:
            cash_delta.iloc[loc:] -= quantity * price
            continue
        if code and code != "CASH" and code not in shares:
            shares[code] = 0.0
            price_matrix[code] = np.nan
        if side == "BUY":
            shares.iloc[loc:, shares.columns.get_loc(code)] += quantity
            cash_delta.iloc[loc:] -= quantity * price + comm
        elif side == "SELL":
            shares.iloc[loc:, shares.columns.get_loc(code)] -= quantity
            cash_delta.iloc[loc:] += quantity * price - comm

    aligned_prices = price_matrix.reindex(columns=shares.columns).ffill().fillna(0.0)
    market_value = (shares * aligned_prices).sum(axis=1)
    cash = initial_cash + cash_delta
    frame = pd.DataFrame(
        {
            "cash": cash,
            "book_value": np.nan,
            "market_value": market_value,
            "nav": cash + market_value,
        },
        index=index,
    )
    return evaluate_asset_value(frame, benchmark=benchmark)


def evaluate_asset_value(
    asset_value: pd.DataFrame,
    *,
    benchmark: pd.Series | None = None,
) -> pd.Series:
    """Compute the metric set used by broker performance reports."""

    metrics = _empty_metrics()
    if asset_value.empty or "nav" not in asset_value or len(asset_value) < 2:
        if not asset_value.empty and "nav" in asset_value:
            metrics["init_cash"] = _first_number(asset_value["nav"])
            metrics["final_nav"] = _last_number(asset_value["nav"])
        return pd.Series(metrics, name="metrics")

    nav = pd.to_numeric(asset_value["nav"], errors="coerce").dropna()
    if len(nav) < 2:
        return pd.Series(metrics, name="metrics")

    returns = nav.pct_change(fill_method=None).dropna()
    init_cash = float(nav.iloc[0])
    final_nav = float(nav.iloc[-1])
    total_return = (final_nav - init_cash) / init_cash if init_cash > 0 else None
    years = _years_elapsed(nav.index)
    annualized_return = (
        (final_nav / init_cash) ** (1.0 / years) - 1.0
        if years and years > 0 and init_cash > 0 and final_nav > 0
        else None
    )
    periods_per_year = max(1.0, (len(nav) - 1) / years) if years and years > 0 else 252
    ann_factor = math.sqrt(periods_per_year)
    volatility_annual = float(returns.std(ddof=1) * ann_factor) if len(returns) > 1 else 0.0
    sharpe_ratio = annualized_return / volatility_annual if annualized_return is not None and volatility_annual else None
    downside = returns[returns < 0]
    downside_std = float(downside.std(ddof=1) * ann_factor) if len(downside) > 1 else 0.0
    sortino_ratio = annualized_return / downside_std if annualized_return is not None and downside_std else None

    running_max = nav.cummax()
    drawdown = nav - running_max
    pct_drawdown = drawdown / running_max
    max_dd = float(drawdown.min())
    max_dd_pct = float(pct_drawdown.min())
    max_dd_date = pct_drawdown.idxmin()
    calmar_ratio = annualized_return / abs(max_dd_pct) if annualized_return is not None and max_dd_pct else None
    win_rate = float((returns > 0).sum()) / len(returns) if len(returns) else None
    gross_profit = float(returns[returns > 0].sum())
    gross_loss = abs(float(returns[returns < 0].sum()))
    profit_factor = gross_profit / gross_loss if gross_loss else None

    net_base = init_cash if init_cash > 0 else float(nav.abs().max())
    net = nav / net_base if net_base else nav * 0.0
    bm_returns = _benchmark_returns(benchmark, nav.index)
    excess_returns = returns.values - bm_returns.values[: len(returns)]
    tracking_error = float(np.std(excess_returns) * ann_factor) if len(excess_returns) else None
    var_5 = float(np.percentile(returns.values, 5)) if len(returns) else None
    cvar_5 = float(returns[returns <= var_5].mean()) if var_5 is not None else None
    beta = float(returns.cov(bm_returns) / bm_returns.var()) if len(returns) > 30 and bm_returns.var() else None
    alpha = float((returns.mean() - beta * bm_returns.mean()) * periods_per_year) if beta is not None else None
    monthly_returns = net.resample("ME").last().pct_change(fill_method=None).fillna(0)

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
            "max_drawdown_date": str(max_dd_date),
            "calmar_ratio": calmar_ratio,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "max_drawdown_period": _drawdown_period(drawdown, pct_drawdown),
            "VaR_5%": var_5,
            "CVaR_5%": cvar_5,
            "beta": beta,
            "alpha": alpha,
            "excess_return": float(pd.Series(1 + excess_returns).cumprod().iloc[-1] - 1) if len(excess_returns) else None,
            "excess_volatility": tracking_error,
            "information_ratio": metrics["excess_return"] / tracking_error if metrics.get("excess_return") is not None and tracking_error else None,
            "skewness": float(returns.skew()) if len(returns) > 2 else None,
            "kurtosis": float(returns.kurtosis()) if len(returns) > 3 else None,
            "day_return_win_rate": win_rate,
            "monthly_return_std": float(monthly_returns.std(ddof=1)) if len(monthly_returns) > 1 else None,
            "monthly_win_rate": float((monthly_returns > 0).sum()) / len(monthly_returns) if len(monthly_returns) else None,
        }
    )
    excess_return = metrics["excess_return"]
    metrics["information_ratio"] = excess_return / tracking_error if excess_return is not None and tracking_error else None
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


def _first_number(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.iloc[0]) if not values.empty else None


def _last_number(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.iloc[-1]) if not values.empty else None


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
