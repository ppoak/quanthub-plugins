"""Xueqiu local account state."""

from __future__ import annotations

import math

from base_strategy.account import BaseAccount
from .models import XueqiuExecution, XueqiuPosition


class XueqiuAccount(BaseAccount):
    """Mutable local account mirrored to a Xueqiu record book."""

    def __init__(self, *, account_id: str, init_cash: float = 0.0) -> None:
        self.account_id = str(account_id)
        self.init_cash = float(init_cash)
        self.cash = float(init_cash)
        self.positions: dict[str, XueqiuPosition] = {}

    @property
    def book_value(self) -> float:
        return sum(position.book_value for position in self.positions.values())

    @property
    def market_value(self) -> float:
        return sum(position.market_value for position in self.positions.values())

    @property
    def net_asset_value(self) -> float:
        return self.cash + self.market_value

    def position(self, code: str) -> XueqiuPosition:
        if code not in self.positions:
            self.positions[code] = XueqiuPosition(code=code)
        return self.positions[code]

    def replace(self, *, cash: float, positions: dict[str, XueqiuPosition]) -> None:
        self.cash = float(cash)
        self.positions = dict(positions)

    def apply_execution(self, execution: XueqiuExecution) -> None:
        if execution.side in {XueqiuExecution.BUY, XueqiuExecution.SELL}:
            self.position(execution.code).apply_execution(execution)
        self.cash += execution.cashflow

    def update_prices(self, prices) -> None:
        """Mark existing positions from a code-to-price mapping."""
        for code, position in self.positions.items():
            if position.quantity and code in prices and math.isfinite(float(prices[code])):
                position.update_price(prices[code])

    def available_quantity(self, code: str, as_of=None) -> float:
        return self.positions.get(code, XueqiuPosition(code=code)).shares_held
