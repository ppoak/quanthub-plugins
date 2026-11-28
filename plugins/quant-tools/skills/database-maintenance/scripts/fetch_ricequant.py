import argparse
import logging
import os
import tarfile
from pathlib import Path

import pandas as pd

from common.ducktable import DuckTable
from common.project import configure_project, configured_data_root
from ricequant.notebook import RiceQuant


def setup_logger(name, level=logging.INFO):
    logger = logging.getLogger(name)
    if logger.hasHandlers():
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("[%(levelname)s]@[%(asctime)s]-[%(name)s]: %(message)s")
    logger.addHandler(logging.StreamHandler())
    for h in logger.handlers:
        h.setFormatter(fmt)
    return logger


def parse_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def dump_data(dump_path: str | Path, dataset_path: str | Path) -> None:
    config = {
        "quotes_day": {
            "partition": "date",
            "keys": ["time", "code"],
        },
        "quotes_min": {
            "partition": "date",
            "keys": ["time", "code"],
        },
        "index_quotes_day": {
            "partition": "date",
            "keys": ["time", "code"],
        },
        "financial_report": {
            "partition": "date",
            "keys": ["time", "code", "account_name"],
        },
        "index_weights": {
            "partition": "date",
            "keys": ["time", "code", "index_code"],
        },
        "industry_mapping": {
            "partition": "date",
            "keys": ["time", "code"],
        },
        "instruments_info": {
            "partition": None,
            "keys": ["code"],
        },
        "ex_factor": {
            "partition": None,
            "keys": ["ex_date", "code"],
        },
    }
    logger = setup_logger("dump_data")
    archive_path = Path(dump_path)
    extracted_dir = archive_path.parent / "data"
    data_root = Path(dataset_path)
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(extracted_dir)
    for file in extracted_dir.glob("*.parquet"):
        matched = False
        for key in config.keys():
            if file.stem.startswith(key):
                df = pd.read_parquet(file)
                if df.empty:
                    logger.info("Skipping empty dataset: %s", file.stem)
                    file.unlink()
                    matched = True
                    break
                table_path = data_root / key
                dt = DuckTable(table_path)
                if part := config[key]["partition"]:
                    source_col = config[key].get("partition_source", "time")
                    if source_col not in df.columns:
                        raise ValueError(
                            f"Partition source column {source_col!r} not found in {file.name}."
                        )
                    df[part] = pd.to_datetime(df[source_col]).dt.strftime("%Y-%m-%d")
                dt.upsert(
                    df, keys=config[key]["keys"], partition_by=[part] if part else None
                )
                file.unlink()
                logger.info("write_done table=%s path=%s rows=%d", key, table_path, len(df))
                matched = True
                break
        if not matched:
            logger.warning(f"File {file.stem} has no dump mapping and was skipped.")
            file.unlink()
    extracted_dir.rmdir()
    archive_path.unlink()


def update_ricequant() -> None:
    download_path = str(Path(os.getenv("DOWNLOAD_PATH") or "downloads/ricequant").resolve())
    setup_logger("RicequantLogger", level=logging.DEBUG)
    rq = RiceQuant(
        os.getenv("CHROME_DRIVER_PATH"),
        headless=parse_bool_env("RICEQUANT_HEADLESS", default=False),
        download_dir=download_path,
    )
    try:
        rq.login(os.getenv("RICEQUANT_USER"), os.getenv("RICEQUANT_PASSWORD"))
        rq.auto_download_data(
            "data_fetcher.ipynb", "data.tar.gz", target_dir=download_path
        )
    finally:
        rq.driver.quit()
    dump_data(
        str(Path(download_path) / "data.tar.gz"),
        configured_data_root("DATASET_PATH"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download and import RiceQuant data.")
    parser.add_argument("--project-root", default=".", help="Target project directory.")
    return parser.parse_args()


def main() -> None:
    configure_project()
    parse_args()
    logger = setup_logger("RicequantMain")
    logger.info("RiceQuant fetch started")
    update_ricequant()
    logger.info("RiceQuant fetch finished successfully")


if __name__ == "__main__":
    main()
