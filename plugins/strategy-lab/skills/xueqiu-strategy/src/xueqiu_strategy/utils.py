"""Xueqiu symbol helpers."""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd


def cash_amount(amount: float | Decimal) -> Decimal:
    """Keep exact cash to four decimal places for local accounting."""
    value = Decimal(str(amount))
    if not value.is_finite():
        raise ValueError("Cash amount must be finite")
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def round_cash(amount: float | Decimal) -> Decimal:
    """Round a cash amount to cents for display and comparisons."""
    return cash_amount(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def trade_amount(price: float, quantity: float, commission: float, *, buy: bool) -> Decimal:
    """Return the signed net trade amount rounded to cents."""
    if not math.isfinite(commission) or commission < 0:
        raise ValueError("Execution commission must be finite and non-negative")
    gross = Decimal(str(price)) * Decimal(str(quantity))
    # Commission callbacks may use binary floats. Retain their meaningful digits
    # before rounding the net amount, so half-cent ties are not rounded down by noise.
    fee = Decimal(format(commission, ".15g")) if isinstance(commission, float) else Decimal(str(commission))
    return cash_amount((-gross if buy else gross) - fee)


def buy_amount(price: float, quantity: float, commission: float) -> Decimal:
    return -trade_amount(price, quantity, commission, buy=True)


def to_xueqiu_symbol(code: str) -> str:
    """Convert QuantHub/RiceQuant style codes to Xueqiu symbols."""
    value = str(code).strip().upper()
    if value.startswith(("SH", "SZ", "BJ")) and len(value) >= 8:
        return value
    if value.endswith(".SH") or value.endswith(".XSHG"):
        return f"SH{value.split('.')[0]}"
    if value.endswith(".SZ") or value.endswith(".XSHE"):
        return f"SZ{value.split('.')[0]}"
    if value.endswith(".BJ"):
        return f"BJ{value.split('.')[0]}"
    return value


def from_xueqiu_symbol(symbol: str) -> str:
    """Convert Xueqiu symbols to QuantHub style codes when possible."""
    value = str(symbol).strip().upper()
    if value.startswith(("SH", "SZ", "BJ")) and len(value) == 8:
        return f"{value[2:]}.{value[:2]}"
    return value



def shanghai_time(value) -> pd.Timestamp:
    """Represent a timestamp as local wall time in Shanghai."""
    stamp = pd.Timestamp(value)
    return stamp.tz_convert("Asia/Shanghai").tz_localize(None) if stamp.tzinfo else stamp
