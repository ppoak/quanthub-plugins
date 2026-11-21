"""Xueqiu order, execution, and position models."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from .utils import round_cash, shanghai_time, trade_amount


@dataclass
class XueqiuExecution:
    """Local mirror of a Xueqiu execution or cash transfer."""

    execution_id: str
    order_id: str = ""
    datetime: Any = None
    side: str = ""
    code: str = ""
    quantity: float = 0.0
    price: float = 0.0
    comm: float = 0.0
    source: str = "xueqiu"
    meta: dict[str, Any] = field(default_factory=dict)
    account_id: str = ""

    BUY = "BUY"
    SELL = "SELL"
    TRANSFER = "TRANSFER"
    WITHDRAW = "WITHDRAW"

    @property
    def timestamp(self) -> Any:
        if self.datetime is None:
            return None
        return shanghai_time(self.datetime).tz_localize("Asia/Shanghai")

    @property
    def cashflow(self) -> float:
        if self.side in {self.BUY, self.SELL}:
            return float(trade_amount(self.price, self.quantity, self.comm, buy=self.side == self.BUY))
        amount = round_cash(Decimal(str(self.quantity)) * Decimal(str(self.price)))
        return float(-amount if self.side == self.WITHDRAW else amount)

    @classmethod
    def _auto_id(cls, prefix: str | None = None) -> str:
        return f"{prefix or cls.__name__}#{uuid4().hex}"


@dataclass
class XueqiuOrder:
    """Local mirror of a Xueqiu transaction submission."""

    order_id: str = ""
    created_at: Any = None
    executed_at: Any = None
    side: str = "BUY"
    code: str = ""
    quantity: float = 0.0
    execution_type: str = "LIMIT"
    limit_price: float | None = None
    status: str = "CREATED"
    filled: float = 0.0
    amount: float = 0.0
    comm: float = 0.0
    executions: list[XueqiuExecution] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    account_id: str = ""
    source: str = "xueqiu"

    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    MARGIN = "MARGIN"

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    BUY = "BUY"
    SELL = "SELL"

    @classmethod
    def _auto_id(cls, user_id: str | None = None) -> str:
        base = f"{cls.__name__}#{uuid4().hex}"
        return f"{base}:{user_id}" if user_id else base

    def attach(self, execution: XueqiuExecution) -> None:
        self.executions.append(execution)
        self.filled += execution.quantity
        self.amount += execution.price * execution.quantity
        self.comm += execution.comm
        self.executed_at = execution.datetime
        self.status = self.FILLED if self.filled >= self.quantity else self.PARTIAL

    @property
    def remaining(self) -> float:
        return max(float(self.quantity) - float(self.filled), 0.0)

    @property
    def order_type(self) -> str:
        return self.execution_type

    @property
    def filled_quantity(self) -> float:
        return self.filled

    @property
    def avg_price(self) -> float:
        return self.amount / self.filled if self.filled else 0.0

    def is_alive(self, as_of: Any = None) -> bool:
        return self.status in {self.CREATED, self.SUBMITTED, self.ACCEPTED, self.PARTIAL}

    def sync_comm(self) -> None:
        self.comm = sum(float(execution.comm) for execution in self.executions)

    def cancel(self) -> None:
        if self.status in {self.CREATED, self.SUBMITTED, self.ACCEPTED, self.PARTIAL}:
            self.status = self.CANCELED


@dataclass
class XueqiuPosition:
    """Local Xueqiu position with execution-backed local accounting."""

    code: str
    quantity: float = 0.0
    cost_price: float = 0.0
    price: float = 0.0
    realized_pnl_value: float = 0.0
    raw: dict[str, Any] | None = None
    executions: list[XueqiuExecution] = field(default_factory=list)

    @property
    def shares_held(self) -> float:
        return self.quantity

    @property
    def last_price(self) -> float:
        """Canonical alias for the latest position mark price."""
        return self.price

    @last_price.setter
    def last_price(self, price: float) -> None:
        self.update_price(price)

    @property
    def book_value(self) -> float:
        return self.quantity * self.cost_price

    @property
    def market_value(self) -> float:
        return self.quantity * self.price

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.book_value

    @property
    def realized_pnl(self) -> float:
        return float(round_cash(self.realized_pnl_value))

    def apply_execution(self, execution: XueqiuExecution) -> None:
        """Apply one locally executed fill to this position."""
        if execution.side == XueqiuExecution.BUY:
            new_quantity = self.quantity + execution.quantity
            if new_quantity > 0:
                total_cost = self.book_value - execution.cashflow
                self.cost_price = total_cost / new_quantity
            self.quantity = new_quantity
        elif execution.side == XueqiuExecution.SELL:
            if execution.quantity > self.quantity + 1e-9:
                raise ValueError(f"sell quantity exceeds current position for {self.code}")
            self.realized_pnl_value += execution.cashflow - execution.quantity * self.cost_price
            self.quantity -= execution.quantity
            if self.quantity <= 1e-9:
                self.quantity = 0.0
                self.cost_price = 0.0
        self.price = float(execution.price)
        self.executions.append(execution)

    def update_price(self, price: float) -> None:
        self.price = float(price)
