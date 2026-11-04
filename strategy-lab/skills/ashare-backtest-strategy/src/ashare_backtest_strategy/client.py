"""A-share strategy-facing trading client."""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from base_strategy.client import BaseClient
from .models import AShareOrder

_UNSET_TIME = object()


class AShareClient(BaseClient):
    """Strategy-facing order client for local A-share simulations."""

    _SEQ: dict[str, int] = {}

    def __init__(self, *, client_id: str | None = None) -> None:
        self.client_id = str(client_id or self._auto_id())
        self.account_id: str | None = None
        self._exchange: Any | None = None
        self._account: Any | None = None
        self.lot_size = 100
        self.enforce_t1 = True

    @classmethod
    def _auto_id(cls) -> str:
        cls._SEQ[cls.__name__] = cls._SEQ.get(cls.__name__, 0) + 1
        return f"{cls.__name__}#{cls._SEQ[cls.__name__]}"

    def connect(self, exchange, *, account_id: str) -> dict[str, Any]:
        """Connect this client to one exchange account and return account properties."""
        if not account_id:
            raise ValueError("account_id is required")
        account = exchange.account(str(account_id))
        self._exchange = exchange
        self._account = account
        self.account_id = str(account.account_id)
        self.lot_size = int(getattr(account, "lot_size", self.lot_size))
        self.enforce_t1 = bool(getattr(account, "enforce_t1", self.enforce_t1))
        return {
            "account_id": self.account_id,
            "lot_size": self.lot_size,
            "enforce_t1": self.enforce_t1,
        }

    @property
    def exchange(self):
        """Connected exchange."""
        if self._exchange is None:
            raise ValueError("AShareClient is not connected to an exchange.")
        return self._exchange

    @property
    def account(self):
        """Connected exchange account."""
        if self._account is None:
            raise ValueError("AShareClient is not connected to an account.")
        return self._account

    def buy(
        self,
        code: str,
        quantity: float,
        *,
        exectype: str = AShareOrder.MARKET,
        limit: Optional[float] = None,
        trigger: Optional[float] = None,
        valid_until: Optional[pd.Timestamp] = None,
        **kwargs,
    ) -> AShareOrder:
        """Create and submit a buy order."""
        return self.submit(
            self._make_order(
                side=AShareOrder.BUY,
                code=code,
                quantity=quantity,
                exectype=exectype,
                limit=limit,
                trigger=trigger,
                valid_until=valid_until,
                **kwargs,
            )
        )

    def sell(
        self,
        code: str,
        quantity: float,
        *,
        exectype: str = AShareOrder.MARKET,
        limit: Optional[float] = None,
        trigger: Optional[float] = None,
        valid_until: Optional[pd.Timestamp] = None,
        **kwargs,
    ) -> AShareOrder:
        """Create and submit a sell order."""
        return self.submit(
            self._make_order(
                side=AShareOrder.SELL,
                code=code,
                quantity=quantity,
                exectype=exectype,
                limit=limit,
                trigger=trigger,
                valid_until=valid_until,
                **kwargs,
            )
        )

    def close(self, code: str, **kwargs) -> AShareOrder:
        """Create and submit a sell order for the current holding quantity."""
        quantity = self.account.position(code).shares_held
        return self.submit(
            self._make_order(
                side=AShareOrder.SELL,
                code=code,
                quantity=quantity,
                **kwargs,
            )
        )

    def order_target_value(
        self,
        code: str,
        target_value: float,
        price: float,
        *,
        exectype: str = AShareOrder.MARKET,
        limit: Optional[float] = None,
        **kwargs,
    ) -> AShareOrder:
        """Create and submit an order targeting a position market value."""
        if price <= 0:
            raise ValueError("price must be positive")
        current_value = self.account.position(code).shares_held * float(price)
        delta_value = float(target_value) - current_value
        side = AShareOrder.BUY if delta_value > 0 else AShareOrder.SELL
        quantity = abs(delta_value) / float(price)
        meta = dict(kwargs.pop("meta", {}) or {})
        meta.update({"target_value": float(target_value), "target_price": float(price)})
        return self.submit(
            self._make_order(
                side=side,
                code=code,
                quantity=quantity,
                exectype=exectype,
                limit=limit,
                meta=meta,
                **kwargs,
            )
        )

    def order_target_percent(
        self,
        code: str,
        percent: float,
        price: float,
        *,
        exectype: str = AShareOrder.MARKET,
        limit: Optional[float] = None,
        **kwargs,
    ) -> AShareOrder:
        """Create and submit an order targeting a percentage of current account NAV."""
        target_value = self.account.net_asset_value * float(percent)
        meta = dict(kwargs.pop("meta", {}) or {})
        meta.update({"target_percent": float(percent), "target_price": float(price)})
        return self.order_target_value(
            code,
            target_value=target_value,
            price=price,
            exectype=exectype,
            limit=limit,
            meta=meta,
            **kwargs,
        )

    def submit(self, order: AShareOrder) -> AShareOrder:
        """Submit one order to an exchange and return the accepted order."""
        return self.exchange.accept(order)

    def cancel(self, order_or_id: AShareOrder | str) -> AShareOrder:
        """Cancel one accepted order through an exchange."""
        return self.exchange.cancel(order_or_id)

    def _make_order(
        self,
        *,
        side: str,
        code: str,
        quantity: float,
        exectype: str = AShareOrder.MARKET,
        limit: Optional[float] = None,
        trigger: Optional[float] = None,
        valid_until: Optional[pd.Timestamp] = None,
        **kwargs,
    ) -> AShareOrder:
        account_id = self._account_id()
        order_id = AShareOrder._auto_id(kwargs.pop("user_id", None))
        meta = dict(kwargs.pop("meta", {}) or {})
        created_at = kwargs.pop("created_at", kwargs.pop("trade_time", _UNSET_TIME))
        if created_at is _UNSET_TIME:
            created_at = pd.Timestamp.now()
        elif not isinstance(created_at, pd.Timestamp):
            created_at = pd.to_datetime(created_at)
        rounded_quantity = self.round_lot_down(quantity)
        return AShareOrder(
            order_id=order_id,
            account_id=account_id,
            client_id=self.client_id,
            source=account_id,
            created_at=created_at,
            side=side,
            code=code,
            quantity=float(rounded_quantity),
            execution_type=exectype,
            limit_price=limit,
            trigger_price=trigger,
            valid_until=valid_until,
            status=AShareOrder.CREATED,
            meta=meta,
            **kwargs,
        )

    def round_lot_down(self, quantity: float) -> int:
        """Round quantity down to the client's lot size."""
        parsed = int(quantity)
        if self.lot_size <= 1:
            return max(parsed, 0)
        return max(parsed // self.lot_size * self.lot_size, 0)

    def _account_id(self) -> str:
        if self.account_id is None:
            raise ValueError("AShareClient is not connected to an account.")
        return self.account_id
