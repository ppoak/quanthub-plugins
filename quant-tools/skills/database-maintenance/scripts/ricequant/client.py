"""
RiceQuant Data Fetcher

从 RiceQuant 服务器获取全市场数据，写入 Hive 分区 parquet，打包为 data.tar.gz。
"""

import os
import tarfile
import time
import uuid
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from joblib import Parallel, delayed

import numpy as np
import pandas as pd
from tqdm.auto import tqdm

from rqdatac import *

# ─────────────────────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────────────────────
ARCHIVE_NAME = "data.tar.gz"
LAST_DATE_FILE = ".last_date"

# 全市场股票列表
COMPREHENSIVE_INDEX = [
    "000905.XSHG",
    "000016.XSHG",
    "000001.XSHG",
    "000300.XSHG",
    "000009.XSHG",
    "000010.XSHG",
    "399012.XSHE",
    "399106.XSHE",
    "399001.XSHE",
    "399300.XSHE",
    "399102.XSHE",
    "000985.XSHG",
    "399101.XSHE",
    "000002.XSHG",
    "000903.XSHG",
    "000904.XSHG",
    "000906.XSHG",
    "000907.XSHG",
    "399005.XSHE",
    "399329.XSHE",
    "399852.XSHE",
    "399985.XSHE",
]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Index Weights
# ─────────────────────────────────────────────────────────────────────────────
def get_index_weights(index_code: str, start_date: str, end_date: str):
    data = index_weights(index_code, start_date=start_date, end_date=end_date)
    if data is None:
        return data
    data = data.reset_index()
    data["index_code"] = index_code
    data.columns = ["time", "code", "weight", "index_code"]
    data = data[["time", "code", "index_code", "weight"]]
    data["code"] = id_convert(data["code"].to_list(), to="normal")
    data["index_code"] = id_convert(data["index_code"].to_list(), to="normal")
    return data


def fetch_index_weights(start: str, stop: str):
    print("[Index Weights]")
    data = []
    for index_code in tqdm(COMPREHENSIVE_INDEX):
        data.append(get_index_weights(index_code, start_date=start, end_date=stop))
    data = pd.concat(data, axis=0)
    data.to_parquet(f"index_weights_{start}_{stop}.parquet")
    print(f"  Written: index_weights_{start}_{stop}.parquet  ({len(data)} rows)")
    return data


# ─────────────────────────────────────────────────────────────────────────────
# 2. Instruments Information
# ─────────────────────────────────────────────────────────────────────────────
def fetch_instruments_info(start: str, stop: str):
    print("[Instruments Information]")
    instruments = all_instruments()
    instruments["listed_date"] = pd.to_datetime(instruments["listed_date"], errors="coerce")
    instruments["de_listed_date"] = pd.to_datetime(instruments["de_listed_date"], errors="coerce")
    instruments = instruments.rename(columns={"order_book_id": "code", "type": "instrument_type"})
    instruments["code"] = id_convert(instruments["code"].to_list(), to="normal")
    instruments = instruments.drop_duplicates(subset=["code"])
    instruments.to_parquet(f"instruments_info_{start}_{stop}.parquet")
    print(f"  Written: instruments_info_{start}_{stop}.parquet  ({len(instruments)} rows)")
    return instruments


# ─────────────────────────────────────────────────────────────────────────────
# 3. Quotes
# ─────────────────────────────────────────────────────────────────────────────
def is_minute_freq(freq: str) -> bool:
    return isinstance(freq, str) and freq.lower().endswith("m")


def clean_num_trades(series: pd.Series) -> pd.Series:
    """
    稳健清洗 num_trades。

    RQData 返回的 num_trades 有时是 93.0 这种 float，
    历史数据里也可能存在 NaN、inf 或小数误差。
    这里统一转成 pandas nullable integer: Int64。
    """
    s = pd.to_numeric(series, errors="coerce")
    s = s.replace([np.inf, -np.inf], np.nan)

    arr = s.to_numpy(dtype="float64", na_value=np.nan)
    valid_mask = np.isfinite(arr)

    data = np.zeros(len(arr), dtype="int64")
    data[valid_mask] = np.rint(arr[valid_mask]).astype("int64")

    na_mask = ~valid_mask

    return pd.Series(
        pd.arrays.IntegerArray(data, na_mask),
        index=series.index,
        name=series.name,
    )


def get_price_data(code, begin: str, end: str, freq: str = "1d"):
    data = get_price(
        order_book_ids=code,
        start_date=begin,
        end_date=end,
        adjust_type="none",
        frequency=freq,
    )

    if data is None:
        return None

    data_post = get_price(
        order_book_ids=code,
        start_date=begin,
        end_date=end,
        adjust_type="post",
        frequency=freq,
    )

    if data_post is not None:
        data = pd.merge(
            data,
            data_post,
            left_index=True,
            right_index=True,
            suffixes=["", "_post"],
        )

    data = data.rename(
        {
            "total_turnover": "amount",
            "total_turnover_post": "amount_post",
        },
        axis=1,
    )

    # 成交笔数不参与复权，只保留原始 num_trades，删除后复权版本
    if "num_trades_post" in data.columns:
        data = data.drop(columns=["num_trades_post"])

    # =====================================================
    # 分钟线
    # =====================================================
    if is_minute_freq(freq):
        # amount 是成交额，不需要复权；分钟线里一般也不保留 amount_post
        if "amount_post" in data.columns:
            data = data.drop(columns=["amount_post"])

        # 注意：这里不再删除 num_trades

        data = data.reset_index().rename(
            columns={
                "order_book_id": "code",
                "datetime": "time",
            }
        )

        data["code"] = id_convert(data["code"].to_list(), to="normal")

        if "num_trades" in data.columns:
            data["num_trades"] = clean_num_trades(data["num_trades"])

        types = {}

        for col in data.columns:
            if col == "code":
                types[col] = "string"
            elif col == "time":
                types[col] = "datetime64[ns]"
            elif col == "num_trades":
                types[col] = "Int64"
            else:
                types[col] = "float64"

        return data.astype(types)

    # =====================================================
    # 日线 / 周线等非分钟线
    # =====================================================
    shares = get_shares(code, start_date=begin, end_date=end)

    if shares is not None:
        data = pd.concat([data, shares], axis=1, join="inner")
        data = data.rename(columns={"total": "total_shares"})

    turnover = get_turnover_rate(
        order_book_ids=code,
        start_date=begin,
        end_date=end,
    )

    if turnover is not None:
        turnover = turnover.add_prefix("turnover_")
        data = pd.concat([data, turnover], axis=1, join="inner")

    # Process suspended and st information
    suspended_success = False
    st_success = False

    try:
        suspended = is_suspended(
            order_book_ids=code,
            start_date=begin,
            end_date=end,
        ).stack().swaplevel()

        suspended.name = "suspended"
        suspended_success = True
    except Exception:
        pass

    try:
        st = is_st_stock(
            order_book_ids=code,
            start_date=begin,
            end_date=end,
        ).stack().swaplevel()

        st.name = "st"
        st_success = True
    except Exception:
        pass

    if suspended_success:
        data = pd.concat([data, suspended], axis=1, join="inner")

    if st_success:
        data = pd.concat([data, st], axis=1, join="inner")

    data = data.reset_index().rename(
        columns={
            "level_0": "code",
            "level_1": "time",
            "order_book_id": "code",
            "date": "time",
        }
    )

    data["code"] = id_convert(data["code"].to_list(), to="normal")

    if "num_trades" in data.columns:
        data["num_trades"] = clean_num_trades(data["num_trades"])

    types = {}

    for col in data.columns:
        if col == "code":
            types[col] = "string"
        elif col == "time":
            types[col] = "datetime64[ns]"
        elif col == "num_trades":
            types[col] = "Int64"
        elif col in {"st", "suspended"}:
            types[col] = "bool"
        else:
            types[col] = "float64"

    return data.astype(types)


def fetch_quotes_day(all_stock: list, start: str, stop: str):
    print("[Quotes Day]")

    parts = 2
    datas = []

    for i in range(parts):
        codes = all_stock[
            i * len(all_stock) // parts : (i + 1) * len(all_stock) // parts
        ]

        data = get_price_data(
            codes,
            start,
            stop,
            freq="1d",
        )

        if data is not None:
            datas.append(data)

    if not datas:
        print("  No data fetched.")
        return None

    data = pd.concat(datas, ignore_index=True)

    out_name = f"quotes_day_{start}_{stop}.parquet"
    data.to_parquet(out_name)

    print(f"  Written: {out_name}  ({len(data)} rows)")
    print(f"  Columns: {data.columns.tolist()}")

    if "num_trades" in data.columns:
        print("  num_trades: included")
    else:
        print("  num_trades: not included")

    return data


def fetch_quotes_day_index(start: str, stop: str):
    print("[Quotes Day Index]")

    data = get_price_data(
        COMPREHENSIVE_INDEX,
        begin=start,
        end=stop,
        freq="1d",
    )

    if data is None:
        print("  No data fetched.")
        return None

    out_name = f"index_quotes_day_{start}_{stop}.parquet"
    data.to_parquet(out_name)

    print(f"  Written: {out_name}  ({len(data)} rows)")
    print(f"  Columns: {data.columns.tolist()}")

    if "num_trades" in data.columns:
        print("  num_trades: included")
    else:
        print("  num_trades: not included")

    return data


def fetch_quotes_min(all_stock: list, start: str, stop: str):
    print("[Quotes Minute]")

    data = get_price_data(
        all_stock,
        start,
        stop,
        freq="1m",
    )

    if data is not None:
        out_name = f"quotes_min_{start}_{stop}.parquet"
        data.to_parquet(out_name)

        print(f"  Written: {out_name}  ({len(data)} rows)")
        print(f"  Columns: {data.columns.tolist()}")

        if "num_trades" in data.columns:
            print("  num_trades: included")
        else:
            print("  num_trades: not included")
    else:
        print("  No data fetched.")

    return data


# ─────────────────────────────────────────────────────────────────────────────
# 4. Financial Report
# ─────────────────────────────────────────────────────────────────────────────
def fetch_all_fields():
    fields = pd.read_html("https://www.ricequant.com/doc/rqdata/python/stock-mod")
    income_sheet_field = fields[2]["字段"]
    balance_sheet_field = fields[3]["字段"]
    cashflow_sheet_field = fields[4]["字段"]
    all_field = []
    for _type in ["_ttm", "_lyr", "_mrq"]:
        suffix = _type + "_0"
        all_field += (income_sheet_field + suffix).to_list() + \
            (balance_sheet_field + suffix).to_list() + \
            (cashflow_sheet_field + suffix).to_list()
    return all_field


def fetch_financial_report(code, start: str, end: str, fields):
    target_start = pd.to_datetime(start).normalize()
    target_end = pd.to_datetime(end).normalize()
    adjstart = target_start - pd.Timedelta("10days")
    df = get_factor(
        order_book_ids=code,
        factor=fields,
        start_date=adjstart,
        end_date=target_end,
    )
    if df is None:
        return None
    if isinstance(code, str):
        # Use the lookback window as a baseline, but emit only non-null changes.
        df_diff = df.ne(df.shift()) & df.notna()
    else:
        raise ValueError("You need to get financial data one-by-one")
    df = (
        df.where(df_diff)
        .reset_index()
        .rename(columns={"order_book_id": "code", "date": "time"})
    )
    df["time"] = pd.to_datetime(df["time"])
    df = df.loc[df["time"].between(target_start, target_end)]
    df = (
        df.melt(id_vars=["code", "time"], var_name="combined", value_name="value")
        .dropna(subset=["value"])
    )
    df[["account_name", "report_type"]] = df["combined"].str.extract(r"(.*)_(lyr|ttm|mrq)_0")
    df = pd.pivot(
        df, index=["code", "time", "account_name"], columns="report_type", values="value"
    ).reset_index()
    df["code"] = id_convert(df["code"].to_list(), to="normal")
    df.columns.name = None
    for col in ["mrq", "ttm", "lyr"]:
        if col not in df.columns:
            df[col] = np.nan
    return df


def fetch_all_financial_reports(all_stock: list, start: str, stop: str):
    print("[Financial Report]")
    all_fields = fetch_all_fields()
    data = Parallel(n_jobs=5, backend="threading")(
        delayed(fetch_financial_report)(stock, start, stop, all_fields) for stock in all_stock
    )
    data = pd.concat(data)
    out_name = f"financial_report_{start}_{stop}.parquet"
    data.to_parquet(out_name)
    print(f"  Written: {out_name}  ({len(data)} rows)")
    return data


# ─────────────────────────────────────────────────────────────────────────────
# 5. Industry Mapping
# ─────────────────────────────────────────────────────────────────────────────
def get_industry_mapping(all_stock: list, start: str, end: str):
    trade_dates = get_trading_dates(start_date=start, end_date=end)
    trade_dates = pd.DataFrame(pd.to_datetime(trade_dates), columns=["trade_date"])
    industry_mapping = []
    for date in tqdm(trade_dates["trade_date"]):
        mapping = get_instrument_industry(
            order_book_ids=all_stock, date=date, source="citics_2019", level=0
        )
        if mapping is not None:
            mapping = mapping.reset_index()
            mapping["time"] = date
            industry_mapping.append(mapping)
    industry_mapping = pd.concat(industry_mapping).rename(columns={"order_book_id": "code"})
    industry_mapping["code"] = id_convert(industry_mapping["code"].to_list(), to="normal")
    return industry_mapping


def fetch_industry_mapping(all_stock: list, start: str, stop: str):
    print("[Industry Mapping]")
    data = get_industry_mapping(all_stock, start, stop)
    out_name = f"industry_mapping_{start}_{stop}.parquet"
    data.to_parquet(out_name)
    print(f"  Written: {out_name}  ({len(data)} rows)")
    return data

# ─────────────────────────────────────────────────────────────────────────────
# 6. Ex Factor
# ─────────────────────────────────────────────────────────────────────────────
def fetch_ex_factor(all_stock: list, end: str):
    raw = get_ex_factor(order_book_ids=all_stock, start_date="2000-01-01", end_date=end).reset_index()
    raw["code"] = id_convert(raw["order_book_id"].to_list(), to="normal")
    for column in ["ex_date", "announcement_date", "ex_end_date"]:
        raw[column] = pd.to_datetime(raw[column], errors="coerce")
    raw.sort_values(["code", "ex_date"], inplace=True)
    raw.reset_index(drop=True, inplace=True)

    df = raw[["ex_date", "code", "announcement_date", "ex_cum_factor",
              "ex_factor", "ex_end_date", "rice_create_tm"]].copy()
    out_name = f"ex_factor_{end}.parquet"
    df.to_parquet(out_name)
    print(f"  Written: {out_name}  ({len(df)} rows)")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# 7. Package & Save last_date
# ─────────────────────────────────────────────────────────────────────────────
def package_and_cleanup():
    print("[Package]")
    archive = Path(ARCHIVE_NAME)
    if archive.exists():
        archive.unlink()

    with tarfile.open(ARCHIVE_NAME, "w:gz") as tar:
        for f in Path(".").glob("*.parquet"):
            tar.add(f)
            f.unlink()

    print(f"  Archived: {ARCHIVE_NAME}")
    print(f"  Removed all .parquet files")


def save_last_date(stop: str):
    last_date_file = Path(LAST_DATE_FILE)
    with open(last_date_file, "w") as f:
        f.write(stop)
    print(f"  Updated: {LAST_DATE_FILE} = {stop}")


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("RiceQuant Data Fetcher")
    print("=" * 60)

    # Determine date range
    last_date_file = Path(LAST_DATE_FILE)
    if last_date_file.exists():
        with open(last_date_file, "r") as f:
            start = (pd.to_datetime(f.read()) + timedelta(days=1)).strftime("%Y%m%d")
    else:
        raise ValueError(f"This is only applicable for update, set {LAST_DATE_FILE} first")

    stop = datetime.today().strftime("%Y%m%d")
    print(f"Date range: {start} → {stop}")
    print()

    # Get all stock list
    all_stock = all_instruments(type="CS", market="cn")
    all_stock = all_stock["order_book_id"].to_list()
    print(f"Total stocks: {len(all_stock)}")
    print()

    # ── Fetchers ─────────────────────────────────────────────────────────────
    fetch_index_weights(start, stop)
    print()

    fetch_instruments_info(start, stop)
    print()

    fetch_quotes_day(all_stock, start, stop)
    print()

    fetch_quotes_day_index(start, stop)
    print()

    fetch_quotes_min(all_stock, start, stop)
    print()

    fetch_all_financial_reports(all_stock, start, stop)
    print()

    fetch_industry_mapping(all_stock, start, stop)
    print()

    fetch_ex_factor(all_stock, stop)
    print()

    # ── Package & Save ───────────────────────────────────────────────────────
    package_and_cleanup()
    save_last_date(stop)

    print()
    print("Done. Archive ready for local Database Maintenance import.")


if __name__ == "__main__":
    main()
