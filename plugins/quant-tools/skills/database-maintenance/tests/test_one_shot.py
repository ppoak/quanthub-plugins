import importlib
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import ProxyHandler
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

DATED_COMMANDS = {
    "eastmoney.fetch_eastmoney_intraday_changes": ("EASTMONEY_INTRADAY_CHANGES", {"page_size": 5000, "retries": 1}),
    "eastmoney.fetch_eastmoney_stock_sentiment": ("EASTMONEY_STOCK_SENTIMENT", {"workers": 8, "retries": 1}),
    "xueqiu.fetch_xueqiu_cube_rebalance": ("XUEQIU_CUBE_REBALANCE", {"workers": 2, "sleep": 0.2, "retries": 1}),
    "xueqiu.fetch_xueqiu_social_count": ("XUEQIU_SOCIAL_COUNT", {"timeout": 30.0, "sleep": 0.2, "retries": 2}),
    "xueqiu.fetch_xueqiu_hot_rank": ("XUEQIU_HOT_RANK", {"hot_rank_types": [12, 22], "retries": 1}),
    "xueqiu.fetch_xueqiu_status": ("XUEQIU_STATUS", {"workers": 2, "batch_size": 20, "max_pages": 20, "candidate_limit": 200}),
}


class OneShotTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.chdir, Path.cwd())
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.project = Path(self.enterContext(TemporaryDirectory()))

    def test_daily_commands_accept_no_date_and_keep_routine_defaults(self):
        zone = ZoneInfo("Asia/Shanghai")
        for module_name, (prefix, defaults) in DATED_COMMANDS.items():
            with self.subTest(module=module_name):
                module = importlib.import_module(f"scripts.{module_name}")
                before = datetime.now(zone).date().isoformat()
                with patch.object(sys, "argv", ["entrypoint"]):
                    args = module.parse_args()
                self.assertIn(args.date, {before, datetime.now(zone).date().isoformat()})
                for attr, value in defaults.items():
                    self.assertEqual(getattr(args, attr), value)
                with patch.dict(os.environ, {f"{prefix}_TABLE": "configured_table"}):
                    with patch.object(sys, "argv", ["entrypoint", "--date", "2026-06-04"]):
                        args = module.parse_args()
                self.assertEqual(args.table_name, "configured_table")
                self.assertEqual(args.date, "2026-06-04")

    def test_eastmoney_writes_to_target_project_using_dotenv_and_cli(self):
        from scripts.eastmoney import fetch_eastmoney_intraday_changes as intraday

        (self.project / ".env").write_text(
            "DATA=custom-data\nEASTMONEY_INTRADAY_CHANGES_TABLE=configured_table\n"
            "EASTMONEY_INTRADAY_CHANGES_PAGE_SIZE=17\n",
            encoding="utf-8",
        )
        payload = {"data": {"tc": 1, "allstock": [{"c": "600519", "t": "8201", "tm": 145900}]}}
        argv = ["entrypoint", "--project-root", str(self.project), "--date", "2026-06-04", "--page-size", "7"]
        with patch.object(sys, "argv", argv), patch.object(
            intraday.EastMoneyDataClient, "stock_changes_page", return_value=payload,
        ) as request:
            intraday.main()
        self.assertEqual(request.call_args.kwargs["page_size"], 7)
        paths = list((self.project / "custom-data/configured_table/date=2026-06-04").glob("*.parquet"))
        self.assertEqual(len(paths), 1)
        self.assertEqual(pq.ParquetFile(paths[0]).read()["event_time"].to_pylist(), ["14:59:00"])

    def test_sentiment_uses_configured_proxy_unless_overridden(self):
        from scripts.eastmoney import fetch_eastmoney_stock_sentiment as sentiment

        configured = "http://127.0.0.1:18081"
        override = "http://127.0.0.1:18082"
        (self.project / ".env").write_text(f"EASTMONEY_MONI_PROXY={configured}\n", encoding="utf-8")
        for options, expected in [([], configured), (["--proxy", override], override)]:
            with self.subTest(options=options):
                argv = ["entrypoint", "--project-root", str(self.project), "--date", "2026-06-04", *options]
                with patch.object(sys, "argv", argv):
                    sentiment.configure_project()
                    client = sentiment.build_client(sentiment.parse_args())
                proxies = [
                    getattr(handler, "proxies") for handler in getattr(client.opener, "handlers")
                    if isinstance(handler, ProxyHandler)
                ]
                self.assertEqual(proxies, [{"http": expected, "https": expected}])

    def test_explicit_date_does_not_use_business_timezone(self):
        os.environ["DATABASE_MAINTENANCE_TIMEZONE"] = "Invalid/Timezone"
        for module_name in DATED_COMMANDS:
            with self.subTest(module=module_name):
                module = importlib.import_module(f"scripts.{module_name}")
                with patch.object(sys, "argv", ["entrypoint", "--date", "2026-06-04"]):
                    self.assertEqual(module.parse_args().date, "2026-06-04")

    def test_help_does_not_use_business_timezone(self):
        os.environ["DATABASE_MAINTENANCE_TIMEZONE"] = "Invalid/Timezone"
        for module_name in DATED_COMMANDS:
            with self.subTest(module=module_name):
                module = importlib.import_module(f"scripts.{module_name}")
                with patch.object(sys, "argv", ["entrypoint", "--help"]), redirect_stdout(StringIO()):
                    with self.assertRaises(SystemExit) as exit_status:
                        module.parse_args()
                self.assertEqual(exit_status.exception.code, 0)

    def test_skill_has_no_scheduler_entrypoint(self):
        self.assertEqual(list((SKILL_ROOT / "scripts").rglob("scheduler.py")), [])

    def test_relogin_checks_accounts_from_target_project_once(self):
        from scripts.xueqiu import email_relogin

        (self.project / "xueqiu.env").write_text("XUEQIU_RELOGIN_TIMEOUT=42\n", encoding="utf-8")
        with patch.object(email_relogin, "load_accounts", wraps=email_relogin.load_accounts) as load_accounts:
            result = email_relogin.main([
                "--project-root", str(self.project), "--env-path", "xueqiu.env", "--check-accounts",
            ])
        self.assertEqual(result, 0)
        load_accounts.assert_called_once_with(Path("xueqiu.env"))
        self.assertEqual(Path.cwd(), self.project.resolve())
        self.assertEqual(os.environ["XUEQIU_RELOGIN_TIMEOUT"], "42")

    def test_huggingface_respects_remote_delete_setting(self):
        from scripts.huggingface import sync_huggingface_bucket as backup

        with patch.object(sys, "argv", ["entrypoint"]):
            self.assertFalse(backup.parse_args().delete_remote_extra)
        with patch.dict(os.environ, {"HUGGINGFACE_BACKUP_DELETE_REMOTE_EXTRA": "yes"}):
            with patch.object(sys, "argv", ["entrypoint"]):
                self.assertTrue(backup.parse_args().delete_remote_extra)
            with patch.object(sys, "argv", ["entrypoint", "--no-delete-remote-extra"]):
                self.assertFalse(backup.parse_args().delete_remote_extra)


if __name__ == "__main__":
    unittest.main()
