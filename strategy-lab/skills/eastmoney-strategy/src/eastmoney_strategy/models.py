"""EastMoney data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass
class EastMoneyOrder:
    """Local mirror of an EastMoney order row or order submission.

    The object records submission metadata and normalized remote status.
    Matching, fills, commissions, slippage, and account accounting remain on the
    EastMoney simulated-trading server.
    """

    order_id: str = ""
    account_id: str = ""
    client_id: str = ""
    created_at: Any = None
    side: str = "BUY"
    code: str = ""
    quantity: float = 0.0
    execution_type: str = "LIMIT"
    limit_price: float | None = None
    trigger_price: float | None = None
    valid_until: Any = None
    status: str = "CREATED"
    filled: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    execution_id: str | None = None

    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    MARGIN = "MARGIN"
    EXPIRED = "EXPIRED"

    LIMIT = "LIMIT"
    BUY = "BUY"
    SELL = "SELL"

    _CLASS_SEQ: ClassVar[dict[str, int]] = {}

    @classmethod
    def _auto_id(cls, user_id: str | None = None) -> str:
        cls._CLASS_SEQ[cls.__name__] = cls._CLASS_SEQ.get(cls.__name__, 0) + 1
        base = f"{cls.__name__}#{cls._CLASS_SEQ[cls.__name__]}"
        return f"{base}:{user_id}" if user_id else base

    def cancel(self) -> None:
        if self.status in {self.CREATED, self.SUBMITTED, self.PARTIAL}:
            self.status = self.CANCELED


@dataclass
class EastMoneyPosition:
    """Normalized mirror of one EastMoney holding row.

    Numeric fields are copied from the remote payload when present. They should
    be treated as server-reported values, not as locally recomputed accounting.
    """

    code: str
    name: str = ""
    quantity: float = 0.0
    available_quantity: float = 0.0
    cost_price: float = 0.0
    last_price: float = 0.0
    market_value: float = 0.0
    holding_profit_loss: float = 0.0
    holding_profit_loss_ratio: float = 0.0
    day_profit_loss: float = 0.0
    day_profit_loss_ratio: float = 0.0
    profit_loss: float = 0.0
    profit_loss_ratio: float = 0.0
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class EastMoneyQuote:
    """Normalized EastMoney quote with level-one to level-five order book data."""

    code: str
    name: str = ""
    latest: float = 0.0
    bids: tuple[tuple[float, float], ...] = ()
    asks: tuple[tuple[float, float], ...] = ()
    raw: dict[str, Any] | None = None

    @property
    def bid1(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def bid1_volume(self) -> float:
        return self.bids[0][1] if self.bids else 0.0

    @property
    def ask1(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def ask1_volume(self) -> float:
        return self.asks[0][1] if self.asks else 0.0

    def bid(self, level: int = 1) -> tuple[float, float]:
        if level < 1 or level > len(self.bids):
            return 0.0, 0.0
        return self.bids[level - 1]

    def ask(self, level: int = 1) -> tuple[float, float]:
        if level < 1 or level > len(self.asks):
            return 0.0, 0.0
        return self.asks[level - 1]

    def order_price(self, side: str, *, level: int = 1) -> float:
        normalized = str(side).upper()
        if normalized == EastMoneyOrder.BUY:
            return self.ask(level)[0] or self.latest
        if normalized == EastMoneyOrder.SELL:
            return self.bid(level)[0] or self.latest
        return self.latest
