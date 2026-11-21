"""Confirm simulated writes, including requests with an unknown outcome."""

from __future__ import annotations

import time

import pandas as pd

from .errors import XueqiuAPIError, XueqiuTransportError
from .utils import round_cash, shanghai_time


_ATTEMPTS = 3


def transfer_ids(client, gid: str) -> set[str]:
    rows = _recent_rows(client, gid, "bank_transfer")
    return {_row_id(row) for row in rows if _row_id(row)}


def confirm_write(client, pending: dict, save, *, confirmed_absent: bool = False) -> dict:
    """Return a successful response or stop with an actionable error."""
    kind, params = pending["kind"], pending["params"]
    gid = params["gid"]
    if pending["in_flight"]:
        result = _reconcile(client, pending, allow_missing=confirmed_absent)
        if result is not None:
            return result
        pending["in_flight"] = False
        save()

    write = client.add_transaction if kind == "transaction" else client.bank_transfer
    for attempt in range(_ATTEMPTS):
        pending["in_flight"] = True
        save()
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
        save()
        if not safe_to_retry:
            return client.ensure_success(result, f"{kind} for gid {gid}")
        if attempt + 1 < _ATTEMPTS:
            time.sleep(0.25 * (attempt + 1))
    raise XueqiuAPIError(f"Xueqiu {kind} for gid {gid} was not sent successfully after {_ATTEMPTS} attempts")


def _reconcile(client, pending: dict, *, allow_missing: bool = False) -> dict | None:
    kind, params = pending["kind"], pending["params"]
    gid = params["gid"]
    missing = False
    for attempt in range(_ATTEMPTS):
        try:
            rows = _recent_rows(client, gid, kind)
        except (OSError, XueqiuAPIError):
            matches, missing = [], False
        else:
            matches = [row for row in rows if _matches(row, pending)]
            missing = not matches
        if len(matches) == 1:
            return {"result_code": client.SUCCESS_CODE, "result_data": matches[0], "reconciled": True}
        if len(matches) > 1:
            break
        if attempt + 1 < _ATTEMPTS:
            time.sleep(0.25 * (attempt + 1))
    if allow_missing and missing:
        return None
    raise XueqiuAPIError(
        f"Xueqiu {kind} for gid {gid} has an unknown outcome; check the ledger before retrying"
    )


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
        return row.get("comment") == params["comment"] and bool(_row_id(row))
    remote_id = _row_id(row)
    if not remote_id or remote_id in pending["prior_ids"]:
        return False
    remote_time = row.get("time")
    date = row.get("date")
    if remote_time is not None:
        date = shanghai_time(pd.to_datetime(remote_time, unit="ms", utc=True, errors="coerce"))
    return (
        str(date)[:10] == params["date"] and str(row.get("type")) == str(params["transfer_type"])
        and row.get("market") == params["market"] and row.get("amount") is not None
        and round_cash(abs(float(row["amount"]))) == round_cash(params["amount"])
    )
