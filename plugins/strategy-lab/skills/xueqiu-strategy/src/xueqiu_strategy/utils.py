"""Xueqiu symbol helpers."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def round_cash(amount: float | Decimal) -> Decimal:
    """Round a cash amount to cents."""
    return Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def buy_amount(price: float, quantity: float, commission: float) -> Decimal:
    """Return the total payable for a buy, rounded to cents."""
    return round_cash(Decimal(str(price)) * Decimal(str(quantity)) + Decimal(str(commission)))


def to_xueqiu_symbol(code: str) -> str:
    """Convert QuantHub/RiceQuant style codes to Xueqiu symbols."""
    value = str(code).strip().upper()
    if value.startswith(("SH", "SZ")) and len(value) >= 8:
        return value
    if value.endswith(".SH") or value.endswith(".XSHG"):
        return f"SH{value.split('.')[0]}"
    if value.endswith(".SZ") or value.endswith(".XSHE"):
        return f"SZ{value.split('.')[0]}"
    return value


def from_xueqiu_symbol(symbol: str) -> str:
    """Convert Xueqiu symbols to QuantHub style codes when possible."""
    value = str(symbol).strip().upper()
    if value.startswith("SH") and len(value) == 8:
        return f"{value[2:]}.SH"
    if value.startswith("SZ") and len(value) == 8:
        return f"{value[2:]}.SZ"
    return value
