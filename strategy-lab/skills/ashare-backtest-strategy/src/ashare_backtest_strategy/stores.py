"""Excel export for A-share account and ledger objects."""

from __future__ import annotations

import pandas as pd

from .ledger import ORDER_SCHEMA, _records_to_frame

__all__ = ["to_excel"]


def to_excel(account, ledger, path: str, exchange=None) -> None:
    """Export an A-share account plus ledger snapshot to an Excel workbook."""
    position_rows = []
    for code, position in account.positions.items():
        position_rows.append(
            {
                "account_id": account.account_id,
                "code": code,
                "shares_held": position.shares_held,
                "book_value": position.book_value,
                "market_value": position.market_value,
                "unrealized_pnl": position.unrealized_pnl,
                "realized_pnl": position.realized_pnl,
                "price": position.price,
            }
        )

    account_rows = [
        {
            "account_id": account.account_id,
            "init_cash": account.init_cash,
            "cash": account.cash,
            "available_cash": account.available_cash,
            "book_value": account.book_value,
            "market_value": account.market_value,
            "net_asset_value": account.net_asset_value,
            "realized_pnl": account.realized_pnl,
            "unrealized_pnl": account.unrealized_pnl,
        }
    ]

    orders = ledger.orders_frame()
    executions = ledger.executions_frame()
    pending = (
        _records_to_frame(exchange.order_queue, schema=ORDER_SCHEMA)
        if exchange is not None
        else pd.DataFrame()
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(account_rows).to_excel(writer, sheet_name="Account", index=False)
        pd.DataFrame(position_rows).to_excel(writer, sheet_name="Position", index=False)
        orders.to_excel(writer, sheet_name="Order", index=False)
        executions.to_excel(writer, sheet_name="Execution", index=False)
        if not pending.empty:
            pending.to_excel(writer, sheet_name="PendingOrder", index=False)
