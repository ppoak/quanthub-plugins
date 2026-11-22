"""Confirm simulated writes, including requests with an unknown outcome."""

from __future__ import annotations

import time
from hashlib import sha256

import pandas as pd

from .errors import XueqiuAPIError, XueqiuTransportError
from .utils import from_xueqiu_symbol, round_cash, shanghai_time


_ATTEMPTS = 3


class DailyReplay:
    """Identify remote writes when rebuilding a day from the previous close."""

    def __init__(self, ledger, scope: tuple, last_close: pd.Timestamp | None) -> None:
        self._ledger = ledger
        self._prefix = f"[ql:{sha256(repr(scope).encode()).hexdigest()[:20]}/"
        self._sequence: dict[str, int] = {}
        self._transfers: list[dict] | None = None
        self._last_close = str(last_close.date()) if last_close is not None else None

    def trade_comment(self, day: str, comment: str) -> str:
        sequence = self._sequence[day] = self._sequence.get(day, 0) + 1
        return f"{comment} {self._prefix}{day}/{sequence}]".strip()

    def find(self, pending: dict) -> dict | None:
        params = pending["params"]
        if pending["kind"] == "transaction":
            return None
        transfers = self._load_transfers()
        matches = [row for row in transfers if _matches(row, {**pending, "prior_ids": ()})]
        if not matches:
            if self._last_close is not None and any(_record_date(row) == params["date"] for row in transfers):
                raise XueqiuAPIError("Replay does not match the previously recorded Xueqiu transfer")
            return None
        if len(matches) != 1:
            raise XueqiuAPIError("Cannot identify one Xueqiu transfer to replay; check the remote ledger")
        row = matches[0]
        transfers.remove(row)
        return row

    def _load_transfers(self) -> list[dict]:
        if self._transfers is None:
            self._transfers = self._ledger.transfer_history(all_pages=True)
        return self._transfers

    def remember_restored_transfer(self, result: dict) -> None:
        data = result.get("result_data")
        if isinstance(data, dict) and (remote_id := _row_id(data)):
            transfers = self._load_transfers()
            transfers[:] = [row for row in transfers if _row_id(row) != remote_id]

    def check_close(self, day: str) -> None:
        if self._last_close is not None and any(
            self._last_close < _record_date(row) <= day for row in self._load_transfers()
            if _record_date(row)[:4].isdigit()
        ):
            raise XueqiuAPIError("Replay left Xueqiu transfers unsettled before the daily close")
        self._sequence.pop(day, None)


def transfer_ids(client, gid: str) -> set[str]:
    rows = _recent_rows(client, gid, "bank_transfer")
    return {_row_id(row) for row in rows if _row_id(row)}


def transaction_ids(client, gid: str) -> set[str]:
    rows = _recent_rows(client, gid, "transaction")
    return {_row_id(row) for row in rows if _row_id(row)}


def confirm_write(client, pending: dict, *, confirmed_absent: bool = False) -> dict:
    """Return a successful response or stop with an actionable error."""
    kind, params = pending["kind"], pending["params"]
    gid = params["gid"]
    if pending["in_flight"]:
        result = _reconcile(client, pending, allow_missing=confirmed_absent)
        if result is not None:
            return result
        pending["in_flight"] = False

    write = client.add_transaction if kind == "transaction" else client.bank_transfer
    for attempt in range(_ATTEMPTS):
        pending["in_flight"] = True
        try:
            result = write(**params)
        except (OSError, XueqiuTransportError):
            return _reconcile(client, pending)
        if result.get("result_code") == client.SUCCESS_CODE:
            return result

        status = result.get("http_status")
        safe_to_retry = result.get("result_code") == client.NETWORK_CODE and (
            result.get("request_not_sent") or status == 429
        )
        if not safe_to_retry and result.get("result_code") == client.NETWORK_CODE and (
            status is None or status in {408, 425} or status >= 500
        ):
            return _reconcile(client, pending)

        pending["in_flight"] = False
        if not safe_to_retry:
            return client.ensure_success(result, f"{kind} for gid {gid}")
        if attempt + 1 < _ATTEMPTS:
            time.sleep(0.25 * (attempt + 1))
    raise XueqiuAPIError(f"Xueqiu {kind} for gid {gid} was not sent successfully after {_ATTEMPTS} attempts")


def _reconcile(client, pending: dict, *, allow_missing: bool = False) -> dict | None:
    kind, params = pending["kind"], pending["params"]
    gid = params["gid"]
    last_error: Exception | None = None
    for attempt in range(_ATTEMPTS):
        try:
            rows = _recent_rows(client, gid, kind)
        except (OSError, XueqiuAPIError) as exc:
            last_error = exc
            if attempt + 1 < _ATTEMPTS:
                time.sleep(0.25 * (attempt + 1))
                continue
            break
        matches = [row for row in rows if _matches(row, pending)]
        if len(matches) == 1:
            return {"result_code": client.SUCCESS_CODE, "result_data": matches[0], "reconciled": True}
        if len(matches) > 1:
            break
        if allow_missing:
            return None
        # A valid, empty ledger response is authoritative for this request.
        # Retrying it only repeats the same read and can delay the unresolved-write stop.
        raise XueqiuAPIError(
            f"Xueqiu {kind} for gid {gid} has an unknown outcome; check the ledger before retrying"
        )
    raise XueqiuAPIError(
        f"Xueqiu {kind} for gid {gid} could not be reconciled after {_ATTEMPTS} reads"
    ) from last_error


def _recent_rows(client, gid: str, kind: str) -> list[dict]:
    if kind == "transaction":
        result, key = client.transaction_records(gid), "transactions"
    else:
        result, key = client.bank_transfer_records(gid), "bank_transfers"
    data = client.ensure_success(result, f"read {kind} for gid {gid}")["result_data"]
    if not isinstance(data, dict):
        raise XueqiuAPIError(f"Missing {kind} ledger payload")
    rows = data.get(key, data.get("list"))
    if not isinstance(rows, list):
        raise XueqiuAPIError(f"Missing {kind} ledger records")
    return rows


def _row_id(row: dict) -> str:
    return str(row.get("tid") or row.get("id") or "")


def _matches(row: dict, pending: dict) -> bool:
    params = pending["params"]
    if pending["kind"] == "transaction":
        remote_id = _row_id(row)
        if not remote_id or remote_id in pending.get("prior_ids", ()):
            return False
        try:
            same_trade = (
                _record_date(row) == params["date"]
                and str(row.get("type")) == str(params["transaction_type"])
                and from_xueqiu_symbol(row.get("symbol", "")) == from_xueqiu_symbol(params["symbol"])
                and float(row.get("shares") or 0) == float(params["shares"])
                and float(row.get("price") or 0) == float(params["price"])
            )
        except (TypeError, ValueError):
            return False
        return same_trade
    remote_id = _row_id(row)
    if not remote_id or remote_id in pending["prior_ids"]:
        return False
    return (
        _record_date(row) == params["date"] and str(row.get("type")) == str(params["transfer_type"])
        and row.get("market") == params["market"] and row.get("amount") is not None
        and round_cash(abs(float(row["amount"]))) == round_cash(params["amount"])
    )


def _record_date(row: dict) -> str:
    remote_time = row.get("time")
    if remote_time is not None:
        return str(shanghai_time(pd.to_datetime(remote_time, unit="ms", utc=True, errors="coerce")))[:10]
    return str(row.get("date"))[:10]
