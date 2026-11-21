"""Local Xueqiu matching, account settlement, and remote record sync."""

from __future__ import annotations

import math
from collections import deque
from typing import Any

import pandas as pd

from base_strategy.exchange import BaseExchange
from .account import XueqiuAccount
from .client import XueqiuClient
from .ledger import XueqiuLedger
from .models import XueqiuExecution as Execution, XueqiuOrder as Order
from .utils import buy_amount, round_cash, shanghai_time, to_xueqiu_symbol


def _zero_commission(order, price, quantity) -> float:
    return 0.0


class XueqiuExchange(BaseExchange):
    """Match locally and optionally publish completed trades to Xueqiu.

    Local facts are committed before remote writes. Failed remote writes raise
    without discarding local fills or automatically retrying a transaction.
    """

    BUY_TRANSACTION = 1
    SELL_TRANSACTION = 2

    def __init__(
        self, client: XueqiuClient, *, gid: str,
        sync_transactions: bool = False, commission=None,
    ) -> None:
        self.client = client
        self.gid = str(gid)
        self.sync_transactions = bool(sync_transactions)
        self.commission = commission or _zero_commission
        self._order_queue: deque[Order] = deque()
        self._order_map: dict[str, Order] = {}
        self._last_orders: list[Order] = []
        self._last_executions: list[Execution] = []
        self._account: XueqiuAccount | None = None
        self._ledger: XueqiuLedger | None = None

    def register(self, account: XueqiuAccount, ledger: XueqiuLedger) -> None:
        """Connect one local account and its canonical ledger."""
        if self._account is not None and self._account is not account:
            raise ValueError("XueqiuExchange is already connected to another account")
        ledger.subscribe(account)
        self._account = account
        self._ledger = ledger

    def account(self, account_id: str | None = None) -> XueqiuAccount:
        """Return the registered local account without a remote request."""
        if self._account is None:
            raise ValueError("XueqiuExchange is not connected to an account and ledger")
        if account_id is not None and str(account_id) != self._account.account_id:
            raise ValueError(f"Unknown account_id: {account_id}")
        return self._account

    @property
    def order_queue(self) -> list[Order]:
        return list(self._order_queue)

    @property
    def last_orders(self) -> list[Order]:
        return list(self._last_orders)

    @property
    def last_executions(self) -> list[Execution]:
        return list(self._last_executions)

    def submit(self, order: Order, **kwargs: Any) -> Order:
        """Accept a local order intent from the connected client."""
        if order.account_id != self.account().account_id:
            raise ValueError("Order account_id does not match the registered account")
        order.status = Order.ACCEPTED
        self._order_queue.append(order)
        self._order_map[order.order_id] = order
        return order

    def record_order(self, order: Order) -> None:
        """Record a terminal local order."""
        self.account()
        self._ledger.record_order(order)

    def cancel(self, order_or_id: Order | str, **kwargs: Any) -> Order:
        """Cancel a pending local order and record its terminal status."""
        order = self._order_map[order_or_id] if isinstance(order_or_id, str) else order_or_id
        if order.account_id != self.account().account_id:
            raise ValueError("Order account_id does not match the registered account")
        order.cancel()
        self._remove_order(order)
        self.record_order(order)
        return order

    def executions(self, **kwargs: Any) -> list[Execution]:
        return self.last_executions

    def sync(self, *, timestamp=None) -> list[Order]:
        """Return the latest orders after immediate settlement."""
        self.account()
        return self.last_orders

    def _settle_execution(self, execution: Execution) -> None:
        self.account().apply_execution(execution)
        self._ledger.record_execution(execution)
        self._last_executions.append(execution)

    def transfer(self, trade_time, amount: float) -> Execution:
        """Record a cash transfer locally, then optionally publish it remotely."""
        account = self.account()
        amount = float(amount)
        if not math.isfinite(amount):
            raise ValueError("Transfer amount must be finite")
        amount = float(round_cash(amount))
        if -amount > account.cash:
            raise ValueError("Transfer exceeds available cash")
        trade_dt = shanghai_time(trade_time)
        execution = Execution(
            execution_id=Execution._auto_id("xueqiu-transfer"),
            account_id=account.account_id, datetime=trade_dt,
            side=Execution.TRANSFER if amount >= 0 else Execution.WITHDRAW,
            code="CASH", quantity=abs(amount), price=1.0,
        )
        self._last_orders = []
        self._last_executions = []
        self._settle_execution(execution)
        if self.sync_transactions:
            result = self.client.bank_transfer(
                gid=self.gid, transfer_type=1 if amount >= 0 else 2,
                date=str(trade_dt.date()), market="CHA", amount=abs(amount),
            )
            execution.meta["xueqiu_response"] = result
            self.client.ensure_success(result, "bank transfer")
        return execution

    def advance(
        self, bar: pd.DataFrame, *, timestamp, commission=None,
        available_quantity=None, commission_rate: float = 0.0, tax_rate: float = 0.0,
    ) -> tuple[list[Order], list[Execution]]:
        """Match queued orders, settle each fill, then mark existing holdings."""
        account = self.account()
        if not isinstance(bar, pd.DataFrame):
            raise TypeError("advance() requires a pd.DataFrame indexed by code")
        bar_dt = shanghai_time(timestamp)
        self._last_orders = []
        self._last_executions = []
        available_quantity = available_quantity or account.available_quantity
        for order in list(self._order_queue):
            if order.code not in bar.index:
                continue
            fill_px = self.match(order, bar.loc[order.code])
            if fill_px is None:
                continue
            fee = float((commission or self.commission)(order, fill_px, order.remaining))
            if not math.isfinite(fee) or fee < 0:
                raise ValueError("Execution commission must be finite and non-negative")
            insufficient = (
                order.side == Order.SELL and available_quantity(order.code, bar_dt) < order.remaining
            ) or (
                order.side == Order.BUY
                and buy_amount(fill_px, order.remaining, fee) > round_cash(account.cash)
            )
            if insufficient:
                order.status = Order.MARGIN
                self._remove_order(order)
                self._last_orders.append(order)
                self.record_order(order)
                continue
            self.record_execution(
                order, trade_time=bar_dt, price=fill_px, commission=fee,
                commission_rate=commission_rate, tax_rate=tax_rate,
            )
        if "close" in bar.columns:
            account.update_prices(bar["close"])
        return self.last_orders, self.last_executions

    def match(self, order: Order, bar_row) -> float | None:
        """Use the opening price or a reachable limit with opening improvement."""
        if float(bar_row.get("volume", 1.0)) <= 0:
            return None
        open_px = float(bar_row.get("open", bar_row.get("close", 0.0)))
        if not math.isfinite(open_px) or open_px <= 0:
            return None
        if order.execution_type == Order.MARKET:
            return open_px
        high_px = float(bar_row.get("high", open_px))
        low_px = float(bar_row.get("low", open_px))
        if order.execution_type == Order.LIMIT and order.limit_price is not None:
            if order.side == Order.BUY and low_px <= order.limit_price:
                return min(open_px, order.limit_price)
            if order.side == Order.SELL and high_px >= order.limit_price:
                return max(open_px, order.limit_price)
        return None

    def execute(
        self, order: Order, *, price: float, quantity: float, timestamp,
        commission=None, commission_rate: float = 0.0, tax_rate: float = 0.0,
    ) -> Execution:
        """Fill a whole local order at an explicit price."""
        if quantity != order.remaining:
            raise ValueError("Xueqiu execution must fill the remaining order quantity")
        fee = float((commission or self.commission)(order, price, quantity))
        return self.record_execution(
            order, trade_time=timestamp, price=price, commission=fee,
            commission_rate=commission_rate, tax_rate=tax_rate,
        )

    def record_execution(
        self, order: Order, *, trade_time, price: float, commission: float = 0.0,
        commission_rate: float = 0.0, tax_rate: float = 0.0,
    ) -> Execution:
        """Settle an explicit local fill before attempting a remote write."""
        account = self.account()
        if self._order_map.get(order.order_id) is not order:
            raise ValueError("Order is not pending on this exchange")
        price, commission = float(price), float(commission)
        if not math.isfinite(price) or price <= 0 or not math.isfinite(commission) or commission < 0:
            raise ValueError("Execution price or commission is invalid")
        if order.side == Order.SELL and order.remaining > account.available_quantity(order.code):
            raise ValueError("Sell quantity exceeds available position")
        if order.side == Order.BUY and buy_amount(price, order.remaining, commission) > round_cash(account.cash):
            raise ValueError("Buy amount exceeds available cash")
        execution = Execution(
            execution_id=Execution._auto_id("xueqiu-local"), order_id=order.order_id,
            account_id=account.account_id, datetime=shanghai_time(trade_time),
            side=order.side, code=order.code, quantity=order.remaining,
            price=price, comm=commission,
        )
        order.attach(execution)
        self._remove_order(order)
        self._last_orders.append(order)
        self._settle_execution(execution)
        if not order.is_alive():
            self.record_order(order)
        if self.sync_transactions:
            self.sync_execution(order, execution, commission_rate=commission_rate, tax_rate=tax_rate)
        return execution

    def sync_execution(
        self, order: Order, execution: Execution, *,
        commission_rate: float = 0.0, tax_rate: float = 0.0,
    ) -> dict[str, Any] | None:
        """Publish one local fill; never retry automatically after an error."""
        if not self.sync_transactions:
            return None
        previous = execution.meta.get("xueqiu_response")
        if previous is not None:
            return self.client.ensure_success(previous, "add transaction")
        result = self.client.add_transaction(
            gid=self.gid, symbol=to_xueqiu_symbol(execution.code),
            shares=int(execution.quantity),
            transaction_type=self.BUY_TRANSACTION if execution.side == Execution.BUY else self.SELL_TRANSACTION,
            date=str(shanghai_time(execution.datetime).date()), price=execution.price,
            tax_rate=tax_rate, commission_rate=commission_rate,
            comment=str(order.meta.get("comment", "")),
        )
        execution.meta["xueqiu_response"] = result
        order.meta["xueqiu_response"] = result
        return self.client.ensure_success(result, "add transaction")

    def _remove_order(self, order: Order) -> None:
        self._order_queue = deque(item for item in self._order_queue if item is not order)
        self._order_map.pop(order.order_id, None)
