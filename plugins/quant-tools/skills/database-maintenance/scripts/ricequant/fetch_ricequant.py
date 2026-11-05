import argparse
import logging
import os
import shutil
import tarfile
import time
import sys
import uuid
from pathlib import Path

import pandas as pd
import duckdb
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts.common.scheduler import configure_project, configured_data_root, record_task_output

# ---------------------------------------------------------------------------
# setup_logger
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Minimal DuckTable: Hive-partitioned Parquet upsert via DuckDB
# ---------------------------------------------------------------------------

class DuckTable:
    """Minimal Hive-partitioned Parquet table using DuckDB."""

    def __init__(self, root_path):
        self.root_path = Path(root_path)
        self.con = duckdb.connect(database=":memory:", read_only=False)
        self._view_name = f"t_{uuid.uuid4().hex[:8]}"

    def columns(self):
        """Return column names from the first parquet file, or empty list."""
        files = list(self.root_path.rglob("data_0.parquet"))
        if not files:
            return []
        return list(duckdb.read_parquet(str(files[0]), hive_partitioning=True).columns)

    def upsert(self, df: pd.DataFrame, keys: list, partition_by=None):
        """Upsert df into the partitioned Parquet directory."""
        if df.duplicated(subset=keys).any():
            raise ValueError("DataFrame contains duplicate rows based on keys.")
        if not list(self.root_path.rglob("*.parquet")):
            self._write_new(df, partition_by)
        else:
            self.refresh()
            self._upsert_existing(df, keys, partition_by)
        self.refresh()

    def refresh(self):
        """Re-register the parquet files as a DuckDB view."""
        files = list(self.root_path.rglob("data_0.parquet"))
        if not files:
            return
        paths = ", ".join("'" + str(f).replace("'", "''") + "'" for f in files)
        self.con.execute(
            f"CREATE OR REPLACE VIEW {self.view_name} AS "
            f"SELECT * FROM read_parquet([{paths}], hive_partitioning=true)"
        )

    @property
    def view_name(self):
        return self._view_name

    def _write_new(self, df, partition_by):
        self.root_path.mkdir(parents=True, exist_ok=True)
        tmpdir = self.root_path.parent / f".tmp_{uuid.uuid4().hex[:8]}"
        try:
            self._write_df_to_dir(df, str(tmpdir), partition_by)
            if self.root_path.exists():
                shutil.rmtree(self.root_path)
            shutil.move(str(tmpdir), str(self.root_path))
        finally:
            if tmpdir.exists():
                shutil.rmtree(tmpdir, ignore_errors=True)

    def _upsert_existing(self, df, keys, partition_by):
        base_cols = self.columns()
        all_cols = ", ".join(f'"{c}"' for c in base_cols)
        key_expr = ", ".join(f'"{k}"' for k in keys)
        tmpdir = self.root_path.parent / f".tmp_{uuid.uuid4().hex[:8]}"

        tmp_name = f"new_{uuid.uuid4().hex[:6]}"
        self.con.register(tmp_name, df)
        parts_tbl = None

        try:
            if not partition_by:
                tmpdir.mkdir(parents=True, exist_ok=True)
                out_path = tmpdir / "data_0.parquet"
                sql = f"""
                    COPY (
                        SELECT {all_cols} FROM (
                            SELECT *, ROW_NUMBER() OVER (PARTITION BY {key_expr} ORDER BY is_new DESC) AS rn
                            FROM (
                                SELECT {all_cols}, 0 AS is_new FROM {self.view_name}
                                UNION ALL
                                SELECT {all_cols}, 1 AS is_new FROM {tmp_name}
                            )
                        ) WHERE rn = 1
                    ) TO '{out_path}' (FORMAT 'parquet', COMPRESSION 'zstd')
                """
                self.con.execute(sql)
                dst = self.root_path / "data_0.parquet"
                if dst.exists():
                    dst.unlink()
                shutil.move(str(out_path), str(dst))
            else:
                # For partitioned tables: only rewrite affected partitions
                parts_tbl = f"parts_{uuid.uuid4().hex[:6]}"
                affected = df[partition_by].drop_duplicates()
                self.con.register(parts_tbl, affected)
                part_cols = ", ".join(f'"{c}"' for c in partition_by)

                sql = f"""
                    COPY (
                        SELECT {all_cols} FROM (
                            SELECT *, ROW_NUMBER() OVER (PARTITION BY {key_expr} ORDER BY is_new DESC) AS rn
                            FROM (
                                SELECT {all_cols}, 0 AS is_new
                                FROM {self.view_name} AS e
                                JOIN {parts_tbl} AS p USING ({part_cols})
                                UNION ALL
                                SELECT {all_cols}, 1 AS is_new FROM {tmp_name}
                            )
                        ) WHERE rn = 1
                    ) TO '{tmpdir}' (FORMAT 'parquet', COMPRESSION 'zstd', PARTITION_BY ({part_cols}))
                """
                self.con.execute(sql)

                for subdir in [d for d in tmpdir.iterdir() if d.is_dir()]:
                    dst = self.root_path / subdir.name
                    if dst.exists():
                        shutil.rmtree(dst)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(subdir), str(dst))
        finally:
            self.con.unregister(tmp_name)
            if parts_tbl:
                self.con.unregister(parts_tbl)
            if tmpdir.exists():
                shutil.rmtree(tmpdir, ignore_errors=True)

    def _write_df_to_dir(self, df, target, partition_by):
        Path(target).mkdir(parents=True, exist_ok=True)
        reg = f"src_{uuid.uuid4().hex[:8]}"
        self.con.register(reg, df)
        opts = ["FORMAT 'parquet'", "COMPRESSION 'zstd'"]
        if partition_by:
            cols = ", ".join(f'"{c}"' for c in partition_by)
            opts.append(f"PARTITION_BY ({cols})")
            sql = f"COPY (SELECT * FROM {reg}) TO '{target}' ({', '.join(opts)})"
        else:
            sql = f"COPY (SELECT * FROM {reg}) TO '{target}/data_0.parquet' ({', '.join(opts)})"
        self.con.execute(sql)
        self.con.unregister(reg)


class RiceQuant:
    """RiceQuant Class for automating data fetching.

    This class allows users to log in to RiceQuant and download the generated files.

    Attributes:
        driver_path (str | None): Optional ChromeDriver path; otherwise auto-managed.
        logger (logging.Logger): Logger for logging operations.
        driver (webdriver.Chrome): The Selenium Chrome driver instance.
    """

    def __init__(
        self,
        driver_path: str | None = None,
        headless: bool = True,
        download_dir: str | None = None,
    ):
        """Initializes the RiceQuant class with driver settings.

        Args:
            driver_path (str | None): Optional ChromeDriver path for manual pinning.
        """
        self.driver_path = driver_path or None
        self.logger = setup_logger("RicequantLogger", level="DEBUG")
        chrome_options = Options()
        if headless:
            chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        if download_dir:
            Path(download_dir).mkdir(parents=True, exist_ok=True)
            chrome_options.add_experimental_option(
                "prefs",
                {
                    "download.default_directory": str(Path(download_dir).resolve()),
                    "download.prompt_for_download": False,
                    "download.directory_upgrade": True,
                    "safebrowsing.enabled": True,
                },
            )

        service = Service(self.driver_path)
        self.driver = webdriver.Chrome(service=service, options=chrome_options)
        if download_dir:
            self.driver.execute_cdp_cmd(
                "Page.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(Path(download_dir).resolve()),
                },
            )
        self.logger.info(
            "Chrome initialized (browser=%s, driver=%s)",
            self.driver.capabilities.get("browserVersion"),
            self.driver.capabilities.get("chrome", {}).get("chromedriverVersion"),
        )

    def login(self, user: str, password: str):
        """Logs in to RiceQuant using user credentials.

        Args:
            user (str): The user's email for RiceQuant.
            password (str): The user's password for RiceQuant.

        Raises:
            Exception: If login fails due to incorrect credentials or website issues.
        """
        self.logger.debug("Visiting https://www.ricequant.com/")
        self.driver.get("https://www.ricequant.com/")
        time.sleep(5)

        login_button = self.driver.find_element(By.CLASS_NAME, "user-status")
        login_button.click()
        self.logger.debug("Attempting to log in")

        # Switch to password login tab
        password_login = self.driver.find_element(
            By.CSS_SELECTOR, ".el-dialog__body > div > div > ul > li:nth-child(2)"
        )
        password_login.click()

        # Fill out the login form
        inputs = self.driver.find_elements(By.CLASS_NAME, "el-input__inner")
        for ipt in inputs:
            if "\u90ae\u7bb1" in ipt.get_attribute("placeholder"):
                account = ipt
            elif "\u5bc6\u7801" in ipt.get_attribute("placeholder"):
                passwd = ipt

        account.send_keys(user)
        passwd.send_keys(password)

        # Click login button
        submit_button = self.driver.find_element(
            By.CSS_SELECTOR, "button.el-button.common-button.btn--submit"
        )
        submit_button.click()
        self.logger.debug("Logged in successfully")
        time.sleep(5)

    def auto_download_data(
        self,
        notebook_name: str,
        data_name: str,
        target_dir: str = ".",
        timeout: float = 1200,
    ) -> str:
        """Downloads the generated file from a specified notebook.

        Args:
            notebook_name (str): The name of the notebook to be executed and downloaded.
            target_dir (str): The directory where downloaded files will be saved.

        Returns:
            str: The file path of the downloaded file.

        Raises:
            Exception: If unable to locate or download the file.
        """
        self.logger.debug(
            f"Redirecting to the research subdomain to run {notebook_name}"
        )
        self.driver.get("https://www.ricequant.com/research/")
        WebDriverWait(self.driver, 30).until(
            EC.presence_of_element_located((By.ID, "notebook_list"))
        )
        research_handle = self.driver.current_window_handle
        research_url = self.driver.current_url

        # Find the desired notebook
        self.logger.debug(f"Finding `{notebook_name}` notebook")
        notebook_item = WebDriverWait(self.driver, 30).until(
            lambda driver: next(
                (
                    item for item in driver.find_elements(
                        By.CSS_SELECTOR, "#notebook_list .list_item.row"
                    )
                    if notebook_name in item.text
                ),
                None,
            ),
            message=f"Notebook {notebook_name} not found.",
        )
        notebook_link = notebook_item.find_element(By.CSS_SELECTOR, "a")

        # Open the notebook
        previous_handles = set(self.driver.window_handles)
        notebook_link.click()
        notebook_handle = WebDriverWait(self.driver, 30).until(
            lambda driver: next(
                iter(set(driver.window_handles) - previous_handles), None
            )
        )
        self.driver.switch_to.window(notebook_handle)
        time.sleep(5)

        # Run all cells
        cell_menu = self.driver.find_element(
            By.CSS_SELECTOR, "#menus > div > div > ul > li:nth-child(5)"
        )
        cell_menu.click()
        run_all = WebDriverWait(self.driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#run_all_cells"))
        )
        self.logger.debug("Starting to run all cells in the notebook")
        run_all.click()
        time.sleep(5)

        # Wait for all cells to finish executing
        unfinished = 0
        execution_deadline = time.monotonic() + 3600
        while True:
            prompts = self.driver.find_elements(By.CSS_SELECTOR, ".prompt.input_prompt")
            unfinished_cur = sum(1 for prompt in prompts if "*" in prompt.text)
            if unfinished_cur != unfinished:
                self.logger.debug(f"Tasks left: {unfinished_cur}/{len(prompts)}")
                unfinished = unfinished_cur
            if unfinished == 0:
                break
            if time.monotonic() >= execution_deadline:
                raise TimeoutError("RiceQuant notebook execution timed out")
            time.sleep(2)

        errors = self.driver.find_elements(By.CSS_SELECTOR, ".output_error")
        if errors:
            details = "\n".join(error.text for error in errors)
            raise RuntimeError(f"RiceQuant notebook execution failed:\n{details}")

        self.logger.debug("All tasks are finished, looking for the generated data file")
        self.driver.close()
        self.driver.switch_to.window(research_handle)
        return str(self.download_generated_file(
            research_url, data_name, target_dir, timeout
        ))

    def download_generated_file(
        self, research_url: str, data_name: str, target_dir: str, timeout: float = 1200
    ) -> Path:
        """Download an existing archive from the research file list."""
        self.driver.get(research_url)
        self.logger.debug("Loading research file list: %s", self.driver.current_url)
        WebDriverWait(self.driver, 30).until(
            EC.presence_of_element_located((By.ID, "notebook_list"))
        )

        # Find the generated file
        file = WebDriverWait(self.driver, 30).until(
            lambda driver: next(
                (
                    item for item in driver.find_elements(
                        By.CSS_SELECTOR, "#notebook_list .list_item.row"
                    )
                    if item.find_element(By.CSS_SELECTOR, ".item_name").text == data_name
                ),
                None,
            ),
            message=f"Generated data file {data_name} not found.",
        )

        checkbox = file.find_element(By.CSS_SELECTOR, "input[type='checkbox']")
        if not checkbox.is_selected():
            checkbox.click()

        # Download the file
        filepath_parent = Path(target_dir).resolve()
        filepath_parent.mkdir(parents=True, exist_ok=True)
        target_file = filepath_parent / data_name
        if target_file.exists():
            previous_file = target_file.with_name(
                f"{data_name}.previous-{uuid.uuid4().hex[:8]}"
            )
            target_file.rename(previous_file)
            self.logger.info("Preserved previous download: %s", previous_file)
        download_button = WebDriverWait(self.driver, 30).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, ".download-button.btn.btn-default.btn-xs")
            )
        )
        download_button.click()
        self.logger.debug("Downloading %s to %s", data_name, target_file)
        presize, cursize = 0, 0
        start_download = time.monotonic()
        while True:
            files = set(filepath_parent.iterdir())
            if target_file in files:
                cursize = target_file.stat().st_size
            if (
                presize == cursize
                and cursize > 0
                and not (filepath_parent / f"{data_name}.crdownload").exists()
            ):
                with tarfile.open(target_file, "r:gz") as archive:
                    members = archive.getmembers()
                    if not members:
                        raise ValueError("Downloaded RiceQuant archive is empty")
                self.logger.info(
                    "Download verified: bytes=%s members=%s", cursize, len(members)
                )
                break
            present_time = time.monotonic()
            if present_time - start_download > timeout:
                raise TimeoutError(f"RiceQuant download timed out: {target_file}")
            presize = cursize
            time.sleep(2)

        return target_file


def dump_data(dump_path: str, dataset_path: str):
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
    dump_path = Path(dump_path)
    dataset_path = Path(dataset_path)
    with tarfile.open(dump_path, "r:gz") as tar:
        tar.extractall(dump_path.parent / "data")
    for file in (dump_path.parent / "data").glob("*.parquet"):
        matched = False
        for key in config.keys():
            if file.stem.startswith(key):
                df = pd.read_parquet(file)
                if df.empty:
                    record_task_output(key, dataset_path / key, 0, "skipped: empty input")
                    logger.info("Skipping empty dataset: %s", file.stem)
                    file.unlink()
                    matched = True
                    break
                dt = DuckTable(dataset_path / key)
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
                record_task_output(key, dataset_path / key, len(df))
                file.unlink()
                logger.info(f"File {file.stem} dumped successfully!")
                matched = True
                break
        if not matched:
            logger.warning(f"File {file.stem} has no dump mapping and was skipped.")
            file.unlink()
    (dump_path.parent / "data").rmdir()
    dump_path.unlink()


def update_ricequant():
    download_path = str(Path(os.getenv("DOWNLOAD_PATH") or "downloads/ricequant").resolve())
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


def main():
    configure_project()
    parser = argparse.ArgumentParser(description="Download and import RiceQuant data.")
    parser.add_argument("--project-root", default=".", help="Target project directory.")
    parser.parse_args()
    logger = setup_logger("RicequantMain")
    logger.info("RiceQuant fetch started")
    update_ricequant()
    logger.info("RiceQuant fetch finished successfully")


if __name__ == "__main__":
    main()
