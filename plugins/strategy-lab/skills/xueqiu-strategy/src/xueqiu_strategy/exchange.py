"""Local Xueqiu matching, account settlement, and remote record sync."""

from __future__ import annotations

import math
from collections import deque
from pathlib import Path
from typing import Any

import pandas as pd

from base_strategy.exchange import BaseExchange
from .account import XueqiuAccount
from .checkpoint import load_checkpoint, restore_execution, restore_order, restore_position, save_checkpoint
from .client import XueqiuClient
from .errors import XueqiuAPIError
from .ledger import XueqiuLedger
from .models import XueqiuExecution as Execution, XueqiuOrder as Order
from .remote import confirm_write, transfer_ids
from .utils import buy_amount, round_cash, shanghai_time, to_xueqiu_symbol


def _zero_commission(order, price, quantity) -> float:
    return 0.0


class XueqiuExchange(BaseExchange):
    """Match locally and optionally publish completed trades to Xueqiu.

    In mirrored mode, only remotely confirmed events change the local account.
    """

    BUY_TRANSACTION = 1
    SELL_TRANSACTION = 2

    def __init__(
        self, client: XueqiuClient, *, gid: str,
        sync_transactions: bool = False, commission=None, checkpoint_path: str | Path | None = None,
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
        self._checkpoint = Path(checkpoint_path) if checkpoint_path is not None else None
        self._pending_write: dict | None = None
        self._active_bar: pd.Timestamp | None = None
        self.last_completed_bar: pd.Timestamp | None = None

    def register(self, account: XueqiuAccount, ledger: XueqiuLedger) -> None:
        """Connect one local account and its canonical ledger."""
        if self._account is not None and self._account is not account:
            raise ValueError("XueqiuExchange is already connected to another account")
        if ledger.gid is not None and ledger.gid != self.gid:
            raise ValueError("Ledger and exchange must use the same gid")
        state = None
        if self._account is None and self._checkpoint is not None and self._checkpoint.exists():
            state = load_checkpoint(self._checkpoint)
            scope = (state["gid"], state["account_id"], state["sync_transactions"])
            if scope != (self.gid, account.account_id, self.sync_transactions):
                raise ValueError("Checkpoint belongs to a different account, gid or sync mode")
        ledger.subscribe(account)
        ledger.gid = self.gid
        self._account = account
        self._ledger = ledger
        if state is not None:
            self._restore(state)

    @property
    def interrupted_bar(self) -> pd.Timestamp | None:
        """Return the unfinished bar to replay when resuming."""
        return self._active_bar

    def _save(self) -> None:
        if self._checkpoint is None:
            return
        account = self.account()
        save_checkpoint(self._checkpoint, {
            "gid": self.gid, "account_id": account.account_id, "sync_transactions": self.sync_transactions,
            "init_cash": account.init_cash, "cash": account.cash, "positions": account.positions,
            "orders": self._ledger.orders, "executions": self._ledger.executions,
            "queue": self.order_queue, "last_orders": [order.order_id for order in self._last_orders],
            "last_executions": [item.execution_id for item in self._last_executions],
            "active_bar": self._active_bar, "last_completed_bar": self.last_completed_bar,
            "pending_write": self._pending_write,
        })

    def _restore(self, state: dict) -> None:
        account = self.account()
        account.init_cash = state["init_cash"]
        account.replace(cash=state["cash"], positions={
            code: restore_position(item) for code, item in state["positions"].items()
        })
        self._ledger.orders = [restore_order(item) for item in state["orders"]]
        self._ledger.executions = [restore_execution(item) for item in state["executions"]]
        self._order_queue = deque(restore_order(item) for item in state["queue"])
        self._order_map = {order.order_id: order for order in self._order_queue}
        orders = {item.order_id: item for item in self._ledger.orders}
        executions = {item.execution_id: item for item in self._ledger.executions}
        self._last_orders = [orders[key] for key in state["last_orders"]]
        self._last_executions = [executions[key] for key in state["last_executions"]]
        self._active_bar = pd.Timestamp(state["active_bar"]) if state["active_bar"] else None
        self.last_completed_bar = pd.Timestamp(state["last_completed_bar"]) if state["last_completed_bar"] else None
        self._pending_write = state["pending_write"]
        if self._pending_write is not None:
            pending = self._pending_write
            pending["execution"] = restore_execution(pending["execution"])

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
        self._require_no_pending()
        if order.account_id != self.account().account_id:
            raise ValueError("Order account_id does not match the registered account")
        order.status = Order.ACCEPTED
        self._order_queue.append(order)
        self._order_map[order.order_id] = order
        self._save()
        return order

    def record_order(self, order: Order) -> None:
        """Record a terminal local order."""
        self.account()
        self._ledger.record_order(order)

    def cancel(self, order_or_id: Order | str, **kwargs: Any) -> Order:
        """Cancel a pending local order and record its terminal status."""
        self._require_no_pending()
        order = self._order_map[order_or_id] if isinstance(order_or_id, str) else order_or_id
        if order.account_id != self.account().account_id:
            raise ValueError("Order account_id does not match the registered account")
        order.cancel()
        self._remove_order(order)
        self.record_order(order)
        self._save()
        return order

    def executions(self, **kwargs: Any) -> list[Execution]:
        return self.last_executions

    def sync(self, *, timestamp=None) -> list[Order]:
        """Return the latest orders after immediate settlement."""
        self.account()
        return self.last_orders

    def _settle_execution(self, execution: Execution) -> None:
        self.account().apply_execution(execution)
        self._last_executions.append(self._ledger.record_execution(execution))

    def transfer(self, trade_time, amount: float) -> Execution:
        """Apply a cash transfer, confirming it first when mirroring to Xueqiu."""
        self._require_no_pending()
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
        if self.sync_transactions:
            params = dict(gid=self.gid, transfer_type=1 if amount >= 0 else 2,
                          date=str(trade_dt.date()), market="CHA", amount=abs(amount))
            self._pending_write = {
                "kind": "bank_transfer", "params": params, "execution": execution,
                "prior_ids": sorted(transfer_ids(self.client, self.gid)), "in_flight": False,
            }
            self.recover_pending()
        else:
            self._settle_execution(execution)
            self._save()
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
        if self._active_bar is not None:
            if bar_dt != self._active_bar:
                raise ValueError("Resume the unfinished bar before advancing to a different timestamp")
        else:
            if self._checkpoint is not None and self.last_completed_bar is not None and bar_dt <= self.last_completed_bar:
                raise ValueError("The bar was already completed in this checkpoint")
            self._active_bar = bar_dt
            self._last_orders = []
            self._last_executions = []
        available_quantity = available_quantity or account.available_quantity
        completed = False
        try:
            if self._pending_write is not None:
                self.recover_pending()
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
                    self._save()
                    continue
                self.record_execution(
                    order, trade_time=bar_dt, price=fill_px, commission=fee,
                    commission_rate=commission_rate, tax_rate=tax_rate,
                )
            completed = True
        finally:
            account.update_prices(bar["close"] if "close" in bar.columns else {})
            if completed:
                self.last_completed_bar = bar_dt
                self._active_bar = None
            self._save()
        return self.last_orders, self.last_executions

    def match(self, order: Order, bar_row) -> float | None:
        """Use the opening price or a reachable limit with opening improvement."""
        volume = bar_row.get("volume", 1.0)
        if pd.isna(volume) or not math.isfinite(float(volume)) or float(volume) <= 0:
            return None
        opening = bar_row.get("open", bar_row.get("close"))
        if pd.isna(opening):
            return None
        open_px = float(opening)
        if not math.isfinite(open_px) or open_px <= 0:
            return None
        if order.execution_type == Order.MARKET:
            return open_px
        if order.execution_type == Order.LIMIT and order.limit_price is not None:
            limit = order.limit_price
            if order.side == Order.BUY:
                low = bar_row.get("low")
                if open_px <= limit:
                    return open_px
                if not pd.isna(low) and 0 < float(low) <= limit:
                    return limit
            elif order.side == Order.SELL:
                high = bar_row.get("high")
                if open_px >= limit:
                    return open_px
                if not pd.isna(high) and math.isfinite(float(high)) and float(high) >= limit:
                    return limit
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
        """Settle immediately after a fill is confirmed."""
        self._require_no_pending()
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
        if self.sync_transactions:
            self.sync_execution(order, execution, commission_rate=commission_rate, tax_rate=tax_rate)
        else:
            self._finish_fill(order, execution)
            self._save()
        return execution

    def _finish_fill(self, order: Order, execution: Execution) -> None:
        order.attach(execution)
        self._remove_order(order)
        self._last_orders.append(order)
        self._settle_execution(execution)
        self.record_order(order)

    def _require_no_pending(self) -> None:
        if self._pending_write is not None:
            raise XueqiuAPIError("Recover the pending Xueqiu write before submitting another action")

    def recover_pending(self, *, confirmed_absent: bool = False) -> Execution | None:
        """Resolve a pending write. Set confirmed_absent only after checking the remote ledger."""
        pending = self._pending_write
        if pending is None:
            return None
        execution = pending["execution"]
        result = confirm_write(self.client, pending, self._save, confirmed_absent=confirmed_absent)
        execution.meta["xueqiu_response"] = result
        if pending["kind"] == "transaction":
            order = self._order_map[pending["order_id"]]
            data = result.get("result_data") or {}
            tid = data.get("tid") if isinstance(data, dict) else None
            tid = tid or result.get("tid")
            if tid:
                execution.meta["xueqiu_tid"] = str(tid)
            order.meta["xueqiu_response"] = result
            self._finish_fill(order, execution)
        else:
            self._settle_execution(execution)
        self._pending_write = None
        self._save()
        return execution

    def sync_execution(
        self, order: Order, execution: Execution, *,
        commission_rate: float = 0.0, tax_rate: float = 0.0,
    ) -> dict[str, Any] | None:
        """Confirm a fill with Xueqiu once, then settle it locally."""
        if not self.sync_transactions:
            return None
        previous = execution.meta.get("xueqiu_response")
        if previous is not None:
            return self.client.ensure_success(previous, "add transaction")
        self._require_no_pending()
        if self._order_map.get(order.order_id) is not order:
            raise ValueError("Order is not pending on this exchange")
        comment = str(order.meta.get("comment", ""))
        execution.meta["xueqiu_comment"] = f"{comment} [ql:{execution.execution_id.split('#')[-1]}]".strip()
        params = dict(
            gid=self.gid, symbol=to_xueqiu_symbol(execution.code), shares=int(execution.quantity),
            transaction_type=self.BUY_TRANSACTION if execution.side == Execution.BUY else self.SELL_TRANSACTION,
            date=str(shanghai_time(execution.datetime).date()), price=execution.price,
            tax_rate=tax_rate, commission_rate=commission_rate, comment=execution.meta["xueqiu_comment"],
        )
        self._pending_write = {
            "kind": "transaction", "params": params, "execution": execution,
            "order_id": order.order_id, "in_flight": False,
        }
        self.recover_pending()
        return execution.meta["xueqiu_response"]

    def _remove_order(self, order: Order) -> None:
        self._order_queue = deque(item for item in self._order_queue if item is not order)
        self._order_map.pop(order.order_id, None)
