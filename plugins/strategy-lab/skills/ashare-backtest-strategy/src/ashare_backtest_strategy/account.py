"""A-share real-time account state."""

from __future__ import annotations

import math
from collections.abc import Mapping

import pandas as pd

from base_strategy.account import BaseAccount
from .models import AShareExecution, ASharePosition

_NO_PRICE = object()


class AShareAccount(BaseAccount):
    """Real-time A-share account state owned by the local exchange."""

    _SEQ: dict[str, int] = {}

    def __init__(
        self,
        *,
        account_id: str | None = None,
        init_cash: float = 0.0,
        lot_size: int = 100,
        enforce_t1: bool = True,
    ) -> None:
        self.account_id = str(account_id or self._auto_id())
        self.init_cash = float(init_cash)
        self.cash = float(init_cash)
        self.lot_size = int(lot_size)
        self.enforce_t1 = bool(enforce_t1)
        self.positions: dict[str, ASharePosition] = {}
        self._market_value = 0.0

    @classmethod
    def _auto_id(cls) -> str:
        cls._SEQ[cls.__name__] = cls._SEQ.get(cls.__name__, 0) + 1
        return f"{cls.__name__}#{cls._SEQ[cls.__name__]}"

    @property
    def available_cash(self) -> float:
        """Current available cash."""
        return self.cash

    @property
    def book_value(self) -> float:
        """Current FIFO book value."""
        return sum(position.book_value for position in self.positions.values())

    @property
    def market_value(self) -> float:
        """Current marked market value."""
        return self._market_value

    @property
    def net_asset_value(self) -> float:
        """Current account net asset value."""
        return self.cash + self.market_value

    @property
    def realized_pnl(self) -> float:
        """Current realized PnL."""
        return sum(position.realized_pnl for position in self.positions.values())

    @property
    def unrealized_pnl(self) -> float:
        """Current unrealized PnL."""
        return sum(position.unrealized_pnl for position in self.positions.values())

    def position(self, code: str) -> ASharePosition:
        """Return the mutable position object for one instrument code."""
        if code not in self.positions:
            self.positions[code] = ASharePosition(code=code)
        return self.positions[code]

    def update_price(self, code: str, price: float) -> None:
        """Update latest mark price for one position."""
        price = float(price)
        if not math.isfinite(price):
            return
        position = self.position(code)
        previous_value = position.market_value
        position.update_price(price)
        self._market_value += position.market_value - previous_value

    def update_prices(self, prices: Mapping[str, float] | pd.Series) -> None:
        """Update marks for existing open positions from one bar's prices."""
        for code, position in self.positions.items():
            quantity = position.shares_held
            if quantity == 0:
                continue
            price = prices.get(code, _NO_PRICE)
            if price is _NO_PRICE:
                continue
            price = float(price)
            if not math.isfinite(price):
                continue
            previous_value = quantity * position.price
            position.update_price(price)
            self._market_value += quantity * position.price - previous_value

    def available_quantity(self, code: str, datetime) -> float:
        """Return sellable quantity for ``code`` at ``datetime``."""
        position = self.position(code)
        return position.available_quantity(datetime, enforce_t1=self.enforce_t1)

    def apply_execution(self, execution: AShareExecution) -> bool:
        """Apply one exchange-confirmed execution to cash and positions."""
        if execution.side == AShareExecution.BUY:
            self.cash += execution.cashflow
            position = self.position(execution.code)
            previous_value = position.market_value
            position.apply_execution(execution)
            position.update_price(execution.price)
            self._market_value += position.market_value - previous_value
            return True
        if execution.side == AShareExecution.SELL:
            self.cash += execution.cashflow
            position = self.position(execution.code)
            previous_value = position.market_value
            position.apply_execution(execution)
            position.update_price(execution.price)
            self._market_value += position.market_value - previous_value
            return True
        if execution.side in {
            AShareExecution.TRANSFER,
            AShareExecution.WITHDRAW,
            AShareExecution.DIVIDEND,
            AShareExecution.SPLIT,
        }:
            self.cash += execution.cashflow
            return True
        return False

    def round_lot_down(self, quantity: float) -> int:
        """Round quantity down to the account lot size."""
        parsed = int(quantity)
        if self.lot_size <= 1:
            return max(parsed, 0)
        return max(parsed // self.lot_size * self.lot_size, 0)

    def __str__(self) -> str:
        return (
            f"AShareAccount({self.account_id}, "
            f"cash={self.cash:,.2f}, "
            f"market_value={self.market_value:,.2f}, "
            f"net_asset_value={self.net_asset_value:,.2f})"
        )

    def __repr__(self) -> str:
        return self.__str__()
