"""Local A-share multi-account exchange simulator."""

from __future__ import annotations

from collections import deque
from math import isfinite

import pandas as pd
from base_strategy.exchange import BaseExchange

from .account import AShareAccount
from .ledger import AShareLedger
from .models import AShareExecution, AShareOrder
from .utils import FixedRateCommission, VolumeShareSlippage


class AShareExchange(BaseExchange):
    """Local exchange that owns A-share matching and account state updates."""

    def __init__(
        self,
        *,
        slippage=None,
        commission=None,
    ) -> None:
        self.slippage_fn = slippage or VolumeShareSlippage()
        self.commission_fn = commission or FixedRateCommission()
        self._accounts: dict[str, AShareAccount] = {}
        self._ledgers: dict[str, AShareLedger] = {}
        self._order_queue: deque[AShareOrder] = deque()
        self._order_map: dict[str, AShareOrder] = {}

    @property
    def order_queue(self) -> list[AShareOrder]:
        """Accepted orders that have not reached a terminal status."""
        return list(self._order_queue)

    def register(
        self, account: AShareAccount, ledger: AShareLedger | None = None
    ) -> AShareAccount:
        """Register one exchange-managed account."""
        if account.account_id in self._accounts:
            raise ValueError(f"Account already registered: {account.account_id}")
        self._accounts[account.account_id] = account
        if ledger is not None:
            ledger.subscribe(account)
            self._ledgers[account.account_id] = ledger
        return account

    def account(self, account_id: str) -> AShareAccount:
        """Return a registered account by id."""
        return self._accounts[str(account_id)]

    def accept(self, order: AShareOrder) -> AShareOrder:
        """Validate and accept one order into the pending queue."""
        account = self._resolve_account(order.account_id)
        order.quantity = account.round_lot_down(order.quantity)
        if order.quantity <= 0:
            order.status = AShareOrder.REJECTED
            self._record_order(order)
            return order
        order.status = AShareOrder.ACCEPTED
        self._order_queue.append(order)
        self._order_map[order.order_id] = order
        return order

    def cancel(self, order_or_id) -> AShareOrder:
        """Cancel an accepted order."""
        order = self._resolve_order(order_or_id)
        order.cancel()
        self._remove_order(order)
        self._record_order(order)
        return order

    def advance(
        self,
        bar: pd.DataFrame,
        *,
        timestamp,
    ) -> tuple[list[AShareOrder], list[AShareExecution]]:
        """Match pending orders against one bar and update account state."""
        if not isinstance(bar, pd.DataFrame):
            raise TypeError(
                "advance() requires a pd.DataFrame indexed by code with "
                "columns: open, high, low, close, volume"
            )

        if self._order_queue:
            pending_codes = {order.code for order in self._order_queue}
            # Arrow string membership converts each candidate separately. Resolve
            # the index once and keep the original row order, including duplicates.
            selected_bar = bar.loc[
                [code in pending_codes for code in bar.index.to_numpy()]
            ]
            # Materialize column labels once, rather than iterating an Arrow
            # string Index for every security in the cross-section.
            columns = tuple(selected_bar.columns)
            bar_map = {
                str(code): dict(zip(columns, row))
                for code, row in zip(
                    selected_bar.index.to_numpy(),
                    selected_bar.itertuples(index=False, name=None),
                )
            }
        else:
            bar_map = {}
        bar_dt = (
            pd.to_datetime(timestamp) if timestamp is not None else pd.Timestamp.now()
        )
        terminal_orders: list[AShareOrder] = []
        executions: list[AShareExecution] = []
        still_pending: deque[AShareOrder] = deque()
        remaining_volume = {
            code: float(row["volume"] or 0.0)
            for code, row in bar_map.items()
            if "volume" in row
        }

        pending_count = len(self._order_queue)
        for _ in range(pending_count):
            order = self._order_queue.popleft()
            if not order.is_alive(bar_dt):
                terminal_orders.append(order)
                self._record_order(order)
                continue

            account = self._resolve_account(order.account_id)
            row = bar_map.get(order.code, {})
            matched = self.match(order, row)
            if matched is not None:
                fill_px, quantity = matched
                if order.code in remaining_volume:
                    quantity = min(quantity, remaining_volume[order.code])
                if quantity <= 0:
                    still_pending.append(order)
                    continue
                commission = self.commission_fn(order, fill_px, quantity)
                execution = self.execute(
                    order, account, fill_px, quantity, bar_dt, commission
                )
                if execution is not None:
                    if order.code in remaining_volume:
                        remaining_volume[order.code] -= execution.quantity
                    executions.append(execution)
                    self._record_execution(execution)
                if order.status == AShareOrder.PARTIAL:
                    still_pending.append(order)
                else:
                    terminal_orders.append(order)
                    self._record_order(order)
            else:
                still_pending.append(order)

        self._order_queue = still_pending
        self._order_map = {order.order_id: order for order in still_pending}
        return terminal_orders, executions

    def execute(
        self,
        order: AShareOrder,
        account: AShareAccount,
        price: float,
        quantity: float,
        timestamp,
        commission: float,
    ) -> AShareExecution | None:
        """Finalize one matched order if the account can settle the execution."""
        if not (
            isfinite(price)
            and price > 0
            and isfinite(quantity)
            and quantity > 0
            and isfinite(commission)
        ):
            order.status = AShareOrder.REJECTED
            return None
        if order.side == AShareOrder.BUY:
            cost = price * quantity + commission
            if cost > account.available_cash + 1e-9:
                order.status = AShareOrder.MARGIN
                return None
        elif account.available_quantity(order.code, timestamp) < quantity:
            order.status = AShareOrder.MARGIN
            return None

        execution = AShareExecution(
            execution_id=AShareExecution._auto_id(),
            order_id=order.order_id,
            account_id=order.account_id,
            client_id=order.client_id,
            source=order.account_id,
            datetime=timestamp,
            side=AShareExecution.BUY
            if order.side == AShareOrder.BUY
            else AShareExecution.SELL,
            code=order.code,
            quantity=quantity,
            price=price,
            comm=commission,
        )
        order.attach(execution)
        account.apply_execution(execution)
        return execution

    def match(self, order: AShareOrder, bar_row: dict) -> tuple[float, float] | None:
        """Return fill price and quantity if the order matches."""
        try:
            open_px = float(bar_row.get("open", bar_row.get("close", 0.0)))
            high_px = float(bar_row.get("high", open_px))
            low_px = float(bar_row.get("low", open_px))
        except (TypeError, ValueError):
            return None
        if not (
            isfinite(open_px)
            and open_px > 0
            and isfinite(high_px)
            and high_px > 0
            and isfinite(low_px)
            and low_px > 0
        ):
            return None
        base_px: float | None = None

        if order.execution_type == AShareOrder.MARKET:
            base_px = open_px
        elif order.execution_type == AShareOrder.LIMIT:
            base_px = self._match_limit(order, high_px, low_px)
        elif order.execution_type == AShareOrder.STOP:
            if order.side == AShareOrder.BUY and high_px >= order.trigger_price:
                base_px = max(open_px, order.trigger_price)
            elif order.side == AShareOrder.SELL and low_px <= order.trigger_price:
                base_px = min(open_px, order.trigger_price)
        elif order.execution_type == AShareOrder.STOPLIMIT:
            base_px = self._match_stoplimit(order, high_px, low_px)
        elif order.execution_type == AShareOrder.TARGET:
            if order.side == AShareOrder.BUY and high_px >= order.trigger_price:
                base_px = max(open_px, order.trigger_price)
            elif order.side == AShareOrder.SELL and low_px <= order.trigger_price:
                base_px = min(open_px, order.trigger_price)
        elif order.execution_type == AShareOrder.TARGETLIMIT:
            base_px = self._match_targetlimit(order, high_px, low_px)

        if base_px is None or not base_px:
            return None
        fill_px = self.slippage_fn(order, dict(bar_row, base_px=base_px))
        quantity = self._match_quantity(order, bar_row)
        if not (
            isfinite(fill_px) and fill_px > 0 and isfinite(quantity) and quantity > 0
        ):
            return None
        if order.execution_type in {
            AShareOrder.LIMIT,
            AShareOrder.STOPLIMIT,
            AShareOrder.TARGETLIMIT,
        }:
            fill_px = (
                min(fill_px, order.limit_price)
                if order.side == AShareOrder.BUY
                else max(fill_px, order.limit_price)
            )
        return fill_px, quantity

    def _match_quantity(self, order: AShareOrder, bar_row: dict) -> float:
        if "volume" not in bar_row:
            return order.remaining
        volume = float(bar_row["volume"] or 0.0)
        if not isfinite(volume) or volume <= 0:
            return 0.0
        return min(order.remaining, volume)

    def _match_limit(
        self, order: AShareOrder, high_px: float, low_px: float
    ) -> float | None:
        if order.side == AShareOrder.BUY and low_px <= order.limit_price:
            return min(high_px, order.limit_price)
        if order.side == AShareOrder.SELL and high_px >= order.limit_price:
            return max(low_px, order.limit_price)
        return None

    def _match_stoplimit(
        self, order: AShareOrder, high_px: float, low_px: float
    ) -> float | None:
        if order.trigger_price is not None:
            triggered = (
                high_px >= order.trigger_price
                if order.side == AShareOrder.BUY
                else low_px <= order.trigger_price
            )
            if not triggered:
                return None
            order.trigger_price = None
        return self._match_limit(order, high_px, low_px)

    def _match_targetlimit(
        self, order: AShareOrder, high_px: float, low_px: float
    ) -> float | None:
        if order.trigger_price is not None:
            if order.side == AShareOrder.BUY and high_px >= order.trigger_price:
                order.trigger_price = None
            elif order.side == AShareOrder.SELL and low_px <= order.trigger_price:
                order.trigger_price = None
            return None
        return self._match_limit(order, high_px, low_px)

    def _resolve_account(self, account_id: str) -> AShareAccount:
        if account_id not in self._accounts:
            raise KeyError(f"Unknown account_id: {account_id}")
        return self._accounts[account_id]

    def _resolve_order(self, order_or_id) -> AShareOrder:
        if isinstance(order_or_id, str):
            return self._order_map[order_or_id]
        return order_or_id

    def _remove_order(self, order: AShareOrder) -> None:
        self._order_queue = deque(
            item for item in self._order_queue if item is not order
        )
        self._order_map.pop(order.order_id, None)

    def _record_order(self, order: AShareOrder) -> None:
        ledger = self._ledgers.get(order.account_id)
        if ledger is not None:
            ledger.record_order(order)

    def _record_execution(self, execution: AShareExecution) -> None:
        ledger = self._ledgers.get(execution.account_id)
        if ledger is not None:
            ledger.record_execution(execution)
