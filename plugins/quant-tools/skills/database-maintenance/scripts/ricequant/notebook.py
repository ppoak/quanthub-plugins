"""Client for running notebooks and downloading artifacts from RiceQuant."""

import logging
import tarfile
import time
import uuid
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


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
        self.logger = logging.getLogger("RicequantLogger")
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
