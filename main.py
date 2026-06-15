"""
main.py
-------
Orchestrates the full pipeline:
  1. Download CSV from Cockpit (browser automation)
  2. Upload CSV to SharePoint (Microsoft Graph API)
  3. Log results

Run manually:
    python main.py
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

from config_loader import load_config
from tableau_downloader import TableauDownloader
from sharepoint_uploader import SharePointUploader

# ---------------------------------------------------------------------------
# Logging — writes to both console and a log file
# ---------------------------------------------------------------------------
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


def main():
    logger.info("=" * 60)
    logger.info("Cockpit → SharePoint automation starting")
    logger.info("=" * 60)

    cfg = load_config()

    # ------------------------------------------------------------------
    # Step 1 — Download CSV from Cockpit
    # ------------------------------------------------------------------
    logger.info("STEP 1: Downloading from Cockpit (Tableau Server) …")
    downloader = TableauDownloader(cfg)
    try:
        downloaded_files = [downloader.download()]
    except Exception as exc:
        logger.error("Download failed: %s", exc)
        sys.exit(1)

    logger.info("Downloaded %d file(s):", len(downloaded_files))
    for f in downloaded_files:
        logger.info("  %s", f)

    # ------------------------------------------------------------------
    # Step 2 — Upload CSV to SharePoint
    # ------------------------------------------------------------------
    logger.info("STEP 2: Uploading to SharePoint …")
    uploader = SharePointUploader(cfg)
    uploaded_urls = uploader.upload_all(downloaded_files)

    logger.info("Uploaded %d file(s):", len(uploaded_urls))
    for u in uploaded_urls:
        logger.info("  %s", u)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    total = len(downloaded_files)
    success = len(uploaded_urls)
    failed = total - success

    logger.info("=" * 60)
    logger.info("Run complete: %d/%d succeeded, %d failed.", success, total, failed)
    logger.info("Log saved to: %s", LOG_FILE.resolve())
    logger.info("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()