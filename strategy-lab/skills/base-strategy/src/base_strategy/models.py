"""Canonical trader communication and state models.

These models define the minimal vocabulary shared by brokers, exchanges,
stores, and evaluators. Backend modules may extend them or expose compatible
fields, but the framework treats these as the canonical concepts:

``Order`` is intent, ``Execution`` is fact, and ``Position`` is broker-owned
current state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Order:
    """Minimal order intent sent from broker to exchange."""

    order_id: str = ""
    created_at: Any = None
    code: str = ""
    side: str = "BUY"
    quantity: float = 0.0
    order_type: str = "MARKET"
    limit_price: float | None = None
    status: str = "CREATED"
    filled_quantity: float = 0.0
    avg_price: float = 0.0
    comm: float = 0.0
    source: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Execution:
    """Minimal execution fact returned from exchange to broker."""

    execution_id: str = ""
    order_id: str = ""
    timestamp: Any = None
    code: str = ""
    side: str = "BUY"
    quantity: float = 0.0
    price: float = 0.0
    comm: float = 0.0
    source: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def cashflow(self) -> float:
        """Signed cash movement represented by this execution."""
        if self.side == "BUY":
            return -(self.price * self.quantity + self.comm)
        if self.side == "SELL":
            return self.price * self.quantity - self.comm
        return self.price * self.quantity


@dataclass
class Position:
    """Minimal broker-owned current position state."""

    code: str = ""
    quantity: float = 0.0
    available_quantity: float = 0.0
    cost_price: float = 0.0
    last_price: float = 0.0
    realized_pnl: float = 0.0
    source: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
