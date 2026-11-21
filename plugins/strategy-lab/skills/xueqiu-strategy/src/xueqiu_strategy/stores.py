"""Excel export helpers for Xueqiu account and ledger states."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .utils import shanghai_time

__all__ = ["to_excel"]


def to_excel(account, ledger, path: str) -> None:
    """Export the current Xueqiu account and ledger state to an Excel workbook.

    ``openpyxl`` is required. Install the ``excel`` extra of ``base-strategy``.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(destination, engine="openpyxl") as writer:
        pd.DataFrame(
            [{"cash": account.cash, "market_value": account.market_value, "nav": account.net_asset_value}]
        ).to_excel(writer, sheet_name="Account", index=False)
        pd.DataFrame(
            [
                {
                    "code": code,
                    "quantity": position.quantity,
                    "cost_price": position.cost_price,
                    "price": position.price,
                    "book_value": position.book_value,
                    "market_value": position.market_value,
                    "unrealized_pnl": position.unrealized_pnl,
                }
                for code, position in account.positions.items()
            ]
        ).to_excel(writer, sheet_name="Position", index=False)
        pd.DataFrame([dict(vars(order), created_at=shanghai_time(order.created_at),
                           executed_at=shanghai_time(order.executed_at)) for order in ledger.orders]).to_excel(
            writer, sheet_name="Order", index=False
        )
        pd.DataFrame([dict(vars(execution), datetime=shanghai_time(execution.datetime))
                      for execution in ledger.executions]).to_excel(
            writer, sheet_name="Execution", index=False
        )
