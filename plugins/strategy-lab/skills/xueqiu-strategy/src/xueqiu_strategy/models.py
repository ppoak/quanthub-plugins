"""Xueqiu order, execution, and position models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar


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

    _CLASS_SEQ: ClassVar[dict[str, int]] = {}

    @property
    def timestamp(self) -> Any:
        return self.datetime

    @property
    def cashflow(self) -> float:
        gross = self.quantity * self.price
        if self.side == self.BUY:
            return -gross - self.comm
        if self.side == self.SELL:
            return gross - self.comm
        return -gross if self.side == self.WITHDRAW else gross

    @classmethod
    def _auto_id(cls, prefix: str | None = None) -> str:
        key = prefix or cls.__name__
        cls._CLASS_SEQ[key] = cls._CLASS_SEQ.get(key, 0) + 1
        return f"{key}#{cls._CLASS_SEQ[key]}"


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

    _CLASS_SEQ: ClassVar[dict[str, int]] = {}

    @classmethod
    def _auto_id(cls, user_id: str | None = None) -> str:
        cls._CLASS_SEQ[cls.__name__] = cls._CLASS_SEQ.get(cls.__name__, 0) + 1
        base = f"{cls.__name__}#{cls._CLASS_SEQ[cls.__name__]}"
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
        return self.realized_pnl_value

    def apply_execution(self, execution: XueqiuExecution) -> None:
        """Apply one locally executed fill to this position."""
        if execution.side == XueqiuExecution.BUY:
            new_quantity = self.quantity + execution.quantity
            if new_quantity > 0:
                total_cost = self.cost_price * self.quantity + execution.price * execution.quantity
                self.cost_price = total_cost / new_quantity
            self.quantity = new_quantity
        elif execution.side == XueqiuExecution.SELL:
            if execution.quantity > self.quantity + 1e-9:
                raise ValueError(f"sell quantity exceeds current position for {self.code}")
            self.realized_pnl_value += execution.quantity * (execution.price - self.cost_price) - execution.comm
            self.quantity -= execution.quantity
            if self.quantity <= 1e-9:
                self.quantity = 0.0
                self.cost_price = 0.0
        self.price = float(execution.price)
        self.executions.append(execution)

    def update_price(self, price: float) -> None:
        self.price = float(price)
