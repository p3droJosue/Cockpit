"""
tableau_downloader.py
---------------------
Logs in to the PepsiCo "Cockpit" Tableau Server, applies the configured
view (either a saved Custom View or explicit filters), and exports the
table as a Crosstab CSV.

IMPORTANT — selectors:
Cockpit is behind PepsiCo's VPN, so the exact DOM cannot be inspected from
outside. The selectors below follow Tableau Server's standard structure and
include text-based fallbacks. The FIRST time you run this, set
headless=False (see _launch_browser) so you can watch the browser and adjust
any selector that doesn't match. Using a saved Custom View avoids almost all
of this fragility — strongly recommended.
"""

import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)


def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)


class TableauDownloader:
    def __init__(self, config: dict):
        self.cfg = config["tableau"]
        self.download_dir = Path(self.cfg["download_dir"])
        self.download_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def download(self) -> Path:
        """Run the full download and return the path to the CSV."""
        with sync_playwright() as p:
            browser, context, page = self._launch_browser(p)
            try:
                self._login(page)

                custom_url = self.cfg.get("custom_view_url", "").strip()
                if custom_url:
                    logger.info("Using saved Custom View (skipping filter steps).")
                    self._open(page, custom_url)
                else:
                    logger.info("No Custom View set — applying explicit filters.")
                    self._open(page, self.cfg["view_url"])
                    self._apply_filters(page)
                    self._set_measure(page)

                dest = self._export_crosstab_csv(page)
                logger.info("Export complete: %s", dest)
                return dest
            finally:
                context.close()
                browser.close()

    # ------------------------------------------------------------------
    # Browser / navigation
    # ------------------------------------------------------------------

    def _launch_browser(self, playwright):
        # First run: set headless=False to watch and fix selectors, then True.
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1680, "height": 950},
        )
        page = context.new_page()
        page.set_default_timeout(self.cfg["page_load_timeout"] * 1000)
        return browser, context, page

    def _login(self, page):
        logger.info("Opening Cockpit and signing in …")
        page.goto(self.cfg["site_url"])

        # PepsiCo uses SSO (Microsoft / SAML). The flow is usually:
        # email → Next → password → Sign in → (optional MFA).
        # Adjust these selectors after watching the first run.
        try:
            page.wait_for_selector('input[type="email"], input[name="loginfmt"]', timeout=20_000)
            email_box = page.query_selector('input[type="email"], input[name="loginfmt"]')
            email_box.fill(self.cfg["email"])
            page.click('input[type="submit"], button[type="submit"]')

            page.wait_for_selector('input[type="password"], input[name="passwd"]', timeout=15_000)
            page.fill('input[type="password"], input[name="passwd"]', self.cfg["password"])
            page.click('input[type="submit"], button[type="submit"]')

            # Possible "Stay signed in?" prompt
            try:
                page.click('input[value="Yes"], #idSIButton9', timeout=8_000)
            except PlaywrightTimeout:
                pass
        except PlaywrightTimeout:
            logger.warning("Standard SSO selectors not found — you may already be "
                           "logged in via SSO, or the flow differs. Continuing.")

        # Wait until a Tableau view is loaded
        page.wait_for_selector("iframe, .tab-widget, #tabViewerToolbarRegion",
                               timeout=self.cfg["page_load_timeout"] * 1000)
        logger.info("Login/landing complete.")

    def _open(self, page, url: str):
        page.goto(url)
        # Wait for the viz to render
        try:
            page.wait_for_selector("iframe, .tab-widget", timeout=self.cfg["page_load_timeout"] * 1000)
        except PlaywrightTimeout:
            logger.warning("Viz container not detected by selector; waiting fixed time.")
        time.sleep(6)  # Cockpit needs time to render the full table

    # ------------------------------------------------------------------
    # Filters (fallback path only)
    # ------------------------------------------------------------------

    def _apply_filters(self, page):
        """
        Apply each filter from config. Single string = pick one value;
        list = multi-select (check each box, then click Apply).
        Tableau quick filters render as clickable widgets labelled with the
        filter caption. These selectors are best-effort; verify on first run.
        """
        for name, value in self.cfg["filters"].items():
            try:
                self._open_filter(page, name)
                if isinstance(value, list):
                    self._select_multi(page, value)
                else:
                    self._select_single(page, value)
                self._apply_filter(page)
                logger.info("Filter set: %s = %s", name, value)
                time.sleep(2)
            except Exception as exc:
                logger.error("Could not set filter '%s' (%s). "
                             "Check the selector for this filter.", name, exc)

    def _open_filter(self, page, name: str):
        # The filter dropdown caret usually sits next to a label with the
        # filter's caption. Try by accessible name, then by nearby text.
        candidates = [
            f'[aria-label="{name}"]',
            f'div[title="{name}"]',
            f'span:has-text("{name}") >> xpath=../..//*[contains(@class,"tabComboBoxButton")]',
        ]
        for sel in candidates:
            el = page.query_selector(sel)
            if el:
                el.click()
                return
        # Last resort: click the text then its sibling dropdown
        page.click(f'text="{name}"')

    def _select_single(self, page, value: str):
        page.click(f'text="{value}"', timeout=10_000)

    def _select_multi(self, page, values: list):
        # First clear "(All)" if it's checked, then check each desired value.
        for v in values:
            try:
                # Click the checkbox row containing this label
                page.click(f'a:has-text("{v}"), label:has-text("{v}")', timeout=8_000)
            except PlaywrightTimeout:
                logger.warning("Multi-select value not found: %s", v)

    def _apply_filter(self, page):
        # Many Cockpit filters have an "Apply" button after selection.
        try:
            page.click('button:has-text("Apply"), a:has-text("Apply")', timeout=5_000)
        except PlaywrightTimeout:
            pass  # Some filters apply instantly

    def _set_measure(self, page):
        """Set the 'Amt' measure dropdown under the PepsiCo logo (top-right)."""
        measure = self.cfg.get("measure")
        if not measure:
            return
        try:
            # This is a parameter/quick filter near the top-right; pick by value.
            page.click('text="Amt"', timeout=8_000)  # open the dropdown
            page.click(f'text="{measure}"', timeout=8_000)
            time.sleep(2)
            logger.info("Measure set: %s", measure)
        except PlaywrightTimeout:
            logger.warning("Could not set measure dropdown to '%s'.", measure)

    # ------------------------------------------------------------------
    # Export: Download → Crosstab → CSV → Download
    # ------------------------------------------------------------------

    def _export_crosstab_csv(self, page) -> Path:
        date_tag = datetime.now().strftime("%Y-%m")
        filename = self.cfg["output_filename"].format(date=date_tag)
        dest = self.download_dir / sanitize_filename(filename)

        # 1) Open the Download toolbar menu (the down-arrow icon)
        self._click_first(page, [
            '[data-tb-test-id="download-ToolbarButton"]',
            'button[aria-label="Download"]',
            '[aria-label="Download"]',
        ], "Download toolbar button")

        time.sleep(1)

        # 2) Choose "Crosstab"
        self._click_first(page, [
            '[data-tb-test-id="download-flyout-DownloadCrosstab-Button"]',
            'text="Crosstab"',
        ], "Crosstab option")

        time.sleep(2)  # crosstab dialog opens

        # 3) In the dialog, select CSV format
        fmt = self.cfg.get("export_format", "CSV").upper()
        try:
            self._click_first(page, [
                f'[data-tb-test-id="export-crosstab-options-dialog-radio-{fmt}-RadioButton"]',
                f'label:has-text("{fmt}")',
                f'text="{fmt}"',
            ], f"{fmt} radio option")
        except Exception:
            logger.warning("Could not find %s radio; the dialog may default to it.", fmt)

        # 4) Click the dialog's Download button and capture the file
        with page.expect_download(timeout=120_000) as dl_info:
            self._click_first(page, [
                '[data-tb-test-id="export-crosstab-export-Button"]',
                'button:has-text("Download")',
                'text="Download"',
            ], "dialog Download button")

        download = dl_info.value
        shutil.move(str(download.path()), str(dest))
        return dest

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _click_first(self, page, selectors: list, label: str):
        """Try a list of selectors in order; click the first that exists."""
        last_err = None
        for sel in selectors:
            try:
                page.click(sel, timeout=8_000)
                return
            except PlaywrightTimeout as e:
                last_err = e
        raise RuntimeError(f"Could not find/click: {label}. Tried: {selectors}") from last_err


# ------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    path = TableauDownloader(cfg).download()
    print(f"\nDownloaded: {path}")