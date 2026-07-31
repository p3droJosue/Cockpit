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

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from config_loader import load_config

logger = logging.getLogger(__name__)


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
            context, page = self._launch_browser(p)
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

    # ------------------------------------------------------------------
    # Browser / navigation
    # ------------------------------------------------------------------

    def _launch_browser(self, playwright):
        # Uses a *persistent* context so the SSO/MFA login from your first
        # run is cached in `user_data_dir` and reused on every subsequent
        # run. `browser_channel` lets us drive your already-installed Edge
        # instead of downloading Chromium (which avoids the corporate-CA
        # cert dance for `playwright install chromium`).
        headless = bool(self.cfg.get("headless", False))
        channel = (self.cfg.get("browser_channel") or "").strip() or None
        user_data_dir = Path(
            self.cfg.get("user_data_dir", "./.playwright-profile")
        ).expanduser().resolve()
        user_data_dir.mkdir(parents=True, exist_ok=True)

        launch_kwargs = {
            "user_data_dir": str(user_data_dir),
            "headless": headless,
            "accept_downloads": True,
            "viewport": {"width": 1680, "height": 950},
        }
        if channel:
            launch_kwargs["channel"] = channel

        context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(self.cfg["page_load_timeout"] * 1000)
        return context, page

    def _login(self, page):
        logger.info("Opening Cockpit …")
        page.goto(self.cfg["site_url"])

        # If the persistent profile already has a valid Okta session, we'll
        # land straight on Tableau and never see a login form — skip auto-
        # fill in that case. Otherwise: fill the User ID + click Log In to
        # trigger the Okta Verify push, then sit on `manual_login_wait` for
        # you to tap "Approve" on your phone.
        email = (self.cfg.get("email") or "").strip()
        password = (self.cfg.get("password") or "").strip()

        if email:
            self._auto_fill_login(page, email, password)
        else:
            logger.info("No credentials configured — relying on cached session "
                        "or your manual login in the launched browser window.")

        manual_login_wait = int(self.cfg.get("manual_login_wait", 0))
        if manual_login_wait > 0:
            logger.info("Waiting %ds for MFA approval on your phone …",
                        manual_login_wait)
            time.sleep(manual_login_wait)

        # Wait for the post-MFA redirect back to Cockpit BEFORE looking for
        # the viz. Without this, if login stalls we sit on secure.pepsico.com
        # and the plain `iframe` selector below matches Okta's hidden
        # <iframe class="hide" data-se="account-chooser">, which never
        # becomes visible and burns the whole timeout.
        try:
            page.wait_for_url(re.compile(r"cockpit\.mypepsico\.com"),
                              timeout=self.cfg["page_load_timeout"] * 1000)
        except PlaywrightTimeout:
            raise RuntimeError(
                "Never redirected to cockpit.mypepsico.com — login stalled "
                "(probably at Okta's Send Push screen). Approve the push, "
                "or check the earlier log lines for which step didn't click."
            )

        # Tableau-specific selectors only — no bare `iframe`, so we can't
        # accidentally match Okta's hidden account-chooser iframe.
        page.wait_for_selector(
            ".tab-widget, #tabViewerToolbarRegion, "
            "iframe[src*='cockpit.mypepsico.com'], "
            "iframe[src*='tableau']",
            state="visible",
            timeout=self.cfg["page_load_timeout"] * 1000,
        )
        logger.info("Login/landing complete.")

    def _auto_fill_login(self, page, email: str, password: str):
        """
        Fill the User ID and click Log In on whichever login flow PepsiCo
        is serving (custom Okta widget on secure.pepsico.com or, as a
        fallback, Microsoft Azure AD). Returns silently if no login form
        appears — that means we landed on a cached session.

        The password step is OPTIONAL: PepsiCo's Okta flow often goes
        straight from User ID → push notification with no password page,
        so we only fill it if a password input actually shows up.
        """
        user_id_selectors = [
            'input[name="identifier"]',         # Okta v2 widget
            'input[name="username"]',           # Okta classic
            '#okta-signin-username',            # Okta classic id
            'input[autocomplete="username"]',
            'input[type="email"]',
            'input[name="loginfmt"]',           # Microsoft Azure AD
        ]
        combined = ", ".join(user_id_selectors)

        try:
            page.wait_for_selector(combined, timeout=15_000)
        except PlaywrightTimeout:
            logger.info("No login form detected — assuming cached session.")
            return

        field = None
        matched = None
        for sel in user_id_selectors:
            field = page.query_selector(sel)
            if field:
                matched = sel
                break
        if not field:
            logger.warning("Login form is up but no matching User ID field. "
                           "You'll need to type it manually.")
            return

        logger.info("Filling User ID (selector: %s) and clicking Log In.", matched)
        field.fill(email)

        submit_selectors = [
            '#okta-signin-submit',
            'input[type="submit"]',
            'button[type="submit"]',
            'button:has-text("Log In")',
            'button:has-text("Sign In")',
            'button:has-text("Next")',
        ]
        self._click_first(page, submit_selectors, "Log In / Next button")

        # PepsiCo Okta: two more screens before the push fires.
        # (Safely skipped on the Microsoft flow — each step times out fast.)
        self._click_okta_verify_flow(page)

        # If a password page appears (Microsoft flow or password-first Okta),
        # fill it; otherwise the Okta push has already gone out.
        if password:
            try:
                page.wait_for_selector(
                    'input[type="password"], input[name="passwd"]',
                    timeout=8_000,
                )
                logger.info("Password step appeared — filling it.")
                page.fill('input[type="password"], input[name="passwd"]', password)
                self._click_first(page, submit_selectors, "password submit button")
            except PlaywrightTimeout:
                logger.info("No password step — Okta push should now be on your phone.")
        else:
            logger.info("No password configured — Okta push should now be on your phone.")

        # Microsoft "Stay signed in?" prompt (harmless on Okta — just times out).
        try:
            page.click('input[value="Yes"], #idSIButton9', timeout=5_000)
        except PlaywrightTimeout:
            pass

    def _click_okta_verify_flow(self, page):
        """
        After the Log In click, PepsiCo's Okta may show one or two extra
        screens before the push fires on your phone:

          1. (Sometimes) "Verify it's you with a security method"
             → click the blue "Select" button next to
               "Login without a password / Using Okta Verify Mobile".
             Skipped when the profile already remembers a chosen method.
          2. "Get a push notification"
             → (optional) tick "Send push automatically" so this screen
               is skipped on future logins, then click "Send Push".
        """
        # Step 1 (optional) — pick the Okta Verify method.
        try:
            page.wait_for_selector(
                'button:has-text("Select"), a:has-text("Select")',
                state="visible",
                timeout=5_000,
            )
            self._click_first(page, [
                'button:has-text("Select")',
                'a:has-text("Select")',
            ], "security-method Select button")
            logger.info("Picked Okta Verify (push) as the security method.")
        except PlaywrightTimeout:
            logger.info("Security-method picker not shown — already chosen.")

        # Step 2 — optionally tick "Send push automatically" for future runs.
        if bool(self.cfg.get("okta_remember_push", True)):
            try:
                checkbox_label = page.query_selector(
                    'label:has-text("Send push automatically")'
                )
                if checkbox_label:
                    checkbox_label.click()
                    logger.info("Ticked 'Send push automatically' — future "
                                "logins will skip this screen.")
            except Exception as exc:
                logger.debug("Could not tick auto-push checkbox: %s", exc)

        # Step 3 — fire the push. Try CSS selectors first, then fall back
        # to an ARIA role match (Playwright's get_by_role uses accessible
        # name, which survives label/text tweaks in the Okta widget).
        if self._try_click_send_push(page):
            logger.info("Push notification sent — tap Approve on your phone.")
        else:
            logger.warning("Send Push button not found — click it manually "
                           "in the browser window.")
            self._dump_debug_state(page, "send_push_not_found")

    def _try_click_send_push(self, page) -> bool:
        """
        Click Okta's "Send Push" button. Returns True on success. Tries
        several CSS selectors, then an ARIA-role fallback, before giving up.
        """
        push_selectors = [
            'button:has-text("Send Push")',
            'input[value="Send Push"]',
            'input[type="submit"][value*="Push"]',
            '[data-se="okta_verify-signed_nonce"] button',
            'input.button.button-primary',
        ]
        # Wait for ANY of them to appear (visible). If none show up in 20s
        # we still try the role-based fallback below before giving up.
        try:
            page.wait_for_selector(
                ", ".join(push_selectors),
                state="visible",
                timeout=20_000,
            )
        except PlaywrightTimeout:
            pass

        for sel in push_selectors:
            try:
                page.click(sel, timeout=3_000)
                logger.info("Clicked Send Push via selector: %s", sel)
                return True
            except PlaywrightTimeout:
                continue

        # ARIA-role fallback — matches by accessible name regardless of tag.
        try:
            page.get_by_role(
                "button", name=re.compile(r"send\s*push", re.I)
            ).click(timeout=5_000)
            logger.info("Clicked Send Push via ARIA role.")
            return True
        except Exception:
            return False

    def _dump_debug_state(self, page, label: str):
        """
        Save a full-page screenshot, the main-frame HTML, AND each child
        frame's HTML (Cockpit's viz lives in an iframe, so the interesting
        markup is usually in a child frame, not the main page).
        """
        debug_dir = Path("logs/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = debug_dir / f"{ts}_{label}"
        try:
            page.screenshot(path=str(stem) + ".png", full_page=True)
            (stem.with_suffix(".html")).write_text(page.content(),
                                                  encoding="utf-8")
            for i, frame in enumerate(page.frames):
                if frame is page.main_frame:
                    continue
                try:
                    frame_html = frame.content()
                except Exception:
                    continue
                frame_path = debug_dir / f"{ts}_{label}_frame{i}.html"
                frame_path.write_text(frame_html, encoding="utf-8")
            logger.info("Saved debug snapshot: %s.png / .html (+ frames)", stem)
        except Exception as exc:
            logger.warning("Could not save debug snapshot: %s", exc)

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

        # Cockpit embeds Tableau in an <iframe> on some routes. Toolbar
        # selectors must run inside that frame or they'll never resolve.
        target = self._tableau_frame(page)

        # 1) Open the Download toolbar menu (top-right icon whose tooltip
        # is "Choose a format to download" on Cockpit).
        try:
            self._click_first(target, [
                '[data-tb-test-id="download-ToolbarButton"]',
                '[aria-label="Choose a format to download"]',
                'button[title="Choose a format to download"]',
                '[title="Choose a format to download"]',
                'button[aria-label*="download" i]',
                'button[title*="download" i]',
                'button[aria-label="Download"]',
                '[aria-label="Download"]',
                '.tab-icon-download',
                'button:has-text("Download")',
            ], "Download toolbar button")
        except RuntimeError:
            self._dump_debug_state(page, "download_button_not_found")
            raise

        time.sleep(1)

        # 2) Choose "Crosstab" from the flyout menu.
        try:
            self._click_first(target, [
                '[data-tb-test-id="download-flyout-DownloadCrosstab-Button"]',
                'button:has-text("Crosstab")',
                'a:has-text("Crosstab")',
                'text="Crosstab"',
            ], "Crosstab option")
        except RuntimeError:
            self._dump_debug_state(page, "crosstab_option_not_found")
            raise

        time.sleep(2)  # crosstab dialog opens

        # 3) The crosstab dialog asks you to pick which sheet to export.
        # For Field Labor Efficiency it's "Volume Cockpit". Skipped if
        # export_sheet is empty (single-sheet views don't show a picker).
        sheet = (self.cfg.get("export_sheet") or "").strip()
        if sheet:
            try:
                self._click_first(target, [
                    f'[data-tb-test-id="crosstab-options-dialog-thumbnail-{sheet}"]',
                    f'[aria-label="{sheet}"]',
                    f'button:has-text("{sheet}")',
                    f'div:has-text("{sheet}")',
                    f'text="{sheet}"',
                ], f"sheet '{sheet}' in Crosstab dialog")
                logger.info("Selected sheet: %s", sheet)
            except RuntimeError:
                self._dump_debug_state(page, "sheet_pick_not_found")
                raise
            time.sleep(1)

        # 4) Select CSV format
        fmt = self.cfg.get("export_format", "CSV").upper()
        try:
            self._click_first(target, [
                f'[data-tb-test-id="export-crosstab-options-dialog-radio-{fmt}-RadioButton"]',
                f'label:has-text("{fmt}")',
                f'text="{fmt}"',
            ], f"{fmt} radio option")
        except Exception:
            logger.warning("Could not find %s radio; dialog may default to it.", fmt)

        # 5) Click the dialog's Download button and capture the file.
        with page.expect_download(timeout=120_000) as dl_info:
            try:
                self._click_first(target, [
                    '[data-tb-test-id="export-crosstab-export-Button"]',
                    'button:has-text("Download")',
                    'text="Download"',
                ], "dialog Download button")
            except RuntimeError:
                self._dump_debug_state(page, "dialog_download_not_found")
                raise

        download = dl_info.value
        shutil.move(str(download.path()), str(dest))
        return dest

    def _tableau_frame(self, page):
        """
        Return the Frame that hosts the Tableau viz. Cockpit wraps it in
        an <iframe title="Data Visualization"> whose src points at
        cockpit.mypepsico.com/views/... The previous URL-iteration
        approach failed because page.frames may not yet reflect the
        iframe's real URL just after navigation — it shows about:blank
        until the child navigation completes. Resolving the frame from
        the DOM element via content_frame() sidesteps that race.
        """
        iframe_selectors = [
            'iframe[title="Data Visualization"]',
            'iframe[src*="cockpit.mypepsico.com/views"]',
            'iframe[src*="/views/"]',
            'iframe[src*="/vizql/"]',
            'iframe[src*="tableau"]',
        ]
        for sel in iframe_selectors:
            try:
                handle = page.wait_for_selector(sel, timeout=5_000)
            except PlaywrightTimeout:
                continue
            if not handle:
                continue
            frame = handle.content_frame()
            if not frame:
                continue
            try:
                frame.wait_for_load_state("domcontentloaded", timeout=15_000)
            except PlaywrightTimeout:
                pass
            logger.info("Tableau viz frame resolved via: %s (url=%s)",
                        sel, frame.url)
            return frame

        logger.warning("No Tableau iframe matched — using main frame.")
        return page.main_frame

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _click_first(self, target, selectors: list, label: str):
        """
        Try a list of selectors in order; click the first that exists.
        `target` may be a Page or a Frame — both expose .click(sel, timeout=...).
        """
        last_err = None
        for sel in selectors:
            try:
                target.click(sel, timeout=8_000)
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