from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eastmoney_strategy import (
    EMAccount,
    EMClient,
    EMExchange,
    EMLedger,
    EastMoneyAuthError,
)
from eastmoney_strategy.login import (
    EastMoneyCredentials,
    EastMoneyQrCode,
    login_with_qr,
)


class EastMoneyAccountTests(unittest.TestCase):
    def test_from_env_loads_shared_connection_settings(self) -> None:
        env_values = {
            "EASTMONEY_MONI_ACCOUNT_ID": "env-portfolio",
            "EASTMONEY_MONI_USER_ID": "env-user",
            "EASTMONEY_MONI_UT_TOKEN": "env-ut-token",
            "EASTMONEY_MONI_CT_TOKEN": "env-ct-token",
            "EASTMONEY_MONI_HTTP_TIMEOUT_SECONDS": "7.5",
            "EASTMONEY_MONI_HTTP_RETRIES": "3",
            "EASTMONEY_MONI_HTTP_RETRY_SLEEP_SECONDS": "0.25",
            "EASTMONEY_MONI_PAGE_SIZE": "37",
            "EASTMONEY_MONI_PENDING_ORDER_PAGE_SIZE": "175",
            "EASTMONEY_MONI_HOLDINGS_PAGE_SIZE": "250",
        }
        with tempfile.TemporaryDirectory() as directory:
            dotenv_path = Path(directory) / ".env"
            dotenv_path.write_text(
                "\n".join(f"{key}={value}" for key, value in env_values.items()) + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                account = EMAccount.from_env(dotenv_path=dotenv_path)
                client = EMClient.from_env(dotenv_path=dotenv_path)
                exchange = EMExchange.from_env(dotenv_path=dotenv_path)
                ledger = EMLedger.from_env(dotenv_path=dotenv_path)

        self.assertEqual(account.account_id, "env-portfolio")
        for component in (account, client, exchange, ledger):
            self.assertEqual(component.timeout, 7.5)
            self.assertEqual(component.retries, 3)
            self.assertEqual(component.retry_sleep, 0.25)
            self.assertEqual(component.page_size, 37)
            self.assertEqual(component.pending_order_page_size, 175)
            self.assertEqual(component.holdings_page_size, 250)

        exchange.register(account, ledger=ledger)
        with (
            patch.object(
                account,
                "_get_account_info",
                return_value={"data": [{"kyye": "1200", "zzc": "2200"}]},
            ),
            patch.object(
                account,
                "_get_holdings",
                return_value={"data": []},
            ) as get_holdings,
        ):
            account.refresh()
        get_holdings.assert_called_once_with(rec_idx=0, rec_cnt=250)

        with patch.object(exchange, "_fetch_all_rows", return_value=[]) as fetch_rows:
            exchange.fetch_orders(account_id=account.account_id)
            self.assertEqual(fetch_rows.call_args.kwargs["rec_cnt"], 37)
            exchange.fetch_pending_orders(account_id=account.account_id)
            self.assertEqual(fetch_rows.call_args.kwargs["rec_cnt"], 175)

        with patch.object(
            ledger, "request_trade", return_value={"data": []}
        ) as request:
            ledger.fetch_orders(start_date="2026-01-01", end_date="2026-01-02")
        self.assertEqual(request.call_args.args[0]["recCnt"], "37")

    def test_qr_login_reads_wait_and_path_settings_from_env(self) -> None:
        env_values = {
            "EASTMONEY_MONI_LOGIN_TIMEOUT_SECONDS": "12.5",
            "EASTMONEY_MONI_LOGIN_POLL_INTERVAL_SECONDS": "0.25",
            "EASTMONEY_MONI_QR_PATH": ".tmp/mock-login-qr.png",
        }
        credentials = EastMoneyCredentials(
            user_id="mock-user",
            ut_token="mock-ut-token",
            ct_token="mock-ct-token",
        )
        qr_code = EastMoneyQrCode(
            image_url="https://example.test/qr",
            image_path=Path(".tmp/mock-login-qr.png"),
        )
        with tempfile.TemporaryDirectory() as directory:
            dotenv_path = Path(directory) / ".env"
            dotenv_path.write_text(
                "\n".join(f"{key}={value}" for key, value in env_values.items()) + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with patch("eastmoney_strategy.login.EastMoneyLoginSession") as session:
                    session.return_value.start.return_value = qr_code
                    session.return_value.wait.return_value = credentials
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = login_with_qr(
                            dotenv_path=dotenv_path,
                            save_env=False,
                        )

        session.assert_called_once_with(
            qr_path=".tmp/mock-login-qr.png",
            timeout=12.5,
            poll_interval=0.25,
        )
        self.assertIs(result, credentials)

    def test_refresh_replaces_cash_and_holdings_from_fixed_responses(self) -> None:
        account = EMAccount(account_id="portfolio", user_id="user", ut_token="token")
        account.available_cash = 10
        account.positions["old"] = account.position("old")
        with (
            patch.object(
                account,
                "_get_account_info",
                return_value={
                    "data": [{"kyye": "1200", "zzc": "2200", "mktVal": "1000"}]
                },
            ),
            patch.object(
                account,
                "_get_holdings",
                return_value={
                    "data": [
                        {
                            "stockCode": "000001",
                            "stkName": "Ping An",
                            "zqsl": "100",
                            "kysl": "50",
                            "cbj": "9",
                            "mktVal": "1000",
                        }
                    ]
                },
            ),
        ):
            account.refresh()

        self.assertEqual(account.available_cash, 1200)
        self.assertEqual(set(account.positions), {"000001"})
        position = account.positions["000001"]
        self.assertEqual(position.quantity, 100)
        self.assertEqual(account.available_quantity("000001"), 50)

    def test_repeated_execution_sync_preserves_distinct_partial_fills(self) -> None:
        account = EMAccount(account_id="portfolio", user_id="user", ut_token="token")
        ledger = EMLedger()
        ledger.subscribe(account)
        row = {
            "code": "000001",
            "side": "buy",
            "quantity": 100,
            "price": 10,
            "date": "2026-09-28",
            "time": "10:00:00",
            "orderId": "order-1",
            "dealId": "fill-1",
        }
        rows = [row, dict(row, dealId="fill-2", time="10:00:01")]
        with patch.object(ledger, "request_trade", return_value={"data": rows}):
            for _ in range(2):
                ledger.fetch_executions(
                    start_date="2026-09-28",
                    end_date="2026-09-28",
                    record=True,
                )
        self.assertEqual(len(ledger.executions), 2)
        self.assertEqual(
            {fill.execution_id for fill in ledger.executions},
            {"portfolio/fill-1", "portfolio/fill-2"},
        )
        self.assertTrue(
            all(
                fill.meta["eastmoney_order_id"] == "order-1"
                for fill in ledger.executions
            )
        )

    def test_execution_sync_without_deal_id_uses_stable_fill_fields(self) -> None:
        account = EMAccount(account_id="portfolio", user_id="user", ut_token="token")
        ledger = EMLedger()
        ledger.subscribe(account)
        row = {
            "code": "000001",
            "side": "buy",
            "quantity": 100,
            "price": 10,
            "date": "2026-09-28",
            "time": "10:00:00",
            "orderId": "order-1",
        }
        with patch.object(ledger, "request_trade", return_value={"data": [row]}):
            for _ in range(2):
                ledger.fetch_executions(
                    start_date="2026-09-28",
                    end_date="2026-09-28",
                    record=True,
                )
        self.assertEqual(len(ledger.executions), 1)

    def test_auth_failure_does_not_replace_current_account_snapshot(self) -> None:
        account = EMAccount(account_id="portfolio", user_id="user", ut_token="token")
        account.available_cash = 321
        account.positions["000001"] = account.position("000001")
        account.positions["000001"].quantity = 100

        with (
            patch.object(
                account, "_get_account_info", return_value={"data": [{"kyye": "999"}]}
            ),
            patch.object(
                account,
                "_get_holdings",
                side_effect=EastMoneyAuthError("token expired"),
            ),
        ):
            with self.assertRaises(EastMoneyAuthError):
                account.refresh()

        self.assertEqual(account.available_cash, 321)
        self.assertEqual(account.positions["000001"].quantity, 100)


if __name__ == "__main__":
    unittest.main()
