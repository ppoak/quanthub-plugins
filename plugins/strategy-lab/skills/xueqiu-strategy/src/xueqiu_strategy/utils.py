"""Xueqiu symbol helpers."""

from __future__ import annotations


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
