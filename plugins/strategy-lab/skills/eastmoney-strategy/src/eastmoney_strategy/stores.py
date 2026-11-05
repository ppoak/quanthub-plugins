"""Excel export helpers for EastMoney mirrors."""

from __future__ import annotations

import pandas as pd

__all__ = ["to_excel"]


def to_excel(account, ledger, path: str) -> None:
    """Export the current EastMoney account mirror to an Excel workbook.

    The workbook contains server-reported account, position, and stored terminal
    order sheets. Call ``exchange.account(account_id)`` for account and holdings.
    Historical terminal orders can be fetched through ``ledger.fetch_orders()``.

    ``openpyxl`` is required. Install the ``excel`` extra of ``base-strategy``.
    """
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(
            [
                {
                    "cash": account.cash,
                    "available_cash": account.available_cash,
                    "frozen_cash": account.frozen_cash,
                    "fund_balance": account.fund_balance,
                    "total_asset": account.total_asset,
                    "market_value": account.market_value,
                    "net_asset_value": account.net_asset_value,
                }
            ]
        ).to_excel(writer, sheet_name="Account", index=False)
        pd.DataFrame(
            [
                {
                    "code": code,
                    "quantity": position.quantity,
                    "name": position.name,
                    "available_quantity": position.available_quantity,
                    "cost_price": position.cost_price,
                    "last_price": position.last_price,
                    "market_value": position.market_value,
                    "holding_profit_loss": position.holding_profit_loss,
                    "holding_profit_loss_ratio": position.holding_profit_loss_ratio,
                    "day_profit_loss": position.day_profit_loss,
                    "day_profit_loss_ratio": position.day_profit_loss_ratio,
                    "profit_loss": position.profit_loss,
                    "profit_loss_ratio": position.profit_loss_ratio,
                }
                for code, position in account.positions.items()
            ]
        ).to_excel(writer, sheet_name="Position", index=False)
        pd.DataFrame([order.__dict__ for order in ledger.orders]).to_excel(
            writer, sheet_name="Order", index=False
        )
