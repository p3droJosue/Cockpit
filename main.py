"""
main.py
-------
Orchestrates the full monthly pipeline:
  1. Download the Cockpit CSV via browser automation.
  2. Clean it (rename columns, filter to previous fiscal month) and
     append it into Volume Cockpit.xlsx in the synced SharePoint folder,
     backing up the previous version first.

Run manually:
    python main.py
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

from config_loader import load_config
from tableau_downloader import TableauDownloader
import data_processor

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
    logger.info("Cockpit monthly pipeline starting")
    logger.info("=" * 60)

    cfg = load_config()

    # ------------------------------------------------------------------
    # Step 1 — Download CSV from Cockpit
    # ------------------------------------------------------------------
    logger.info("STEP 1: Downloading from Cockpit (Tableau Server) …")
    try:
        csv_path = TableauDownloader(cfg).download()
    except Exception as exc:
        logger.error("Download failed: %s", exc)
        sys.exit(1)
    logger.info("Downloaded: %s", csv_path)

    # ------------------------------------------------------------------
    # Step 2 — Clean CSV and append into Volume Cockpit.xlsx
    # ------------------------------------------------------------------
    logger.info("STEP 2: Cleaning CSV and appending to Volume Cockpit.xlsx …")
    try:
        summary = data_processor.process(csv_path, cfg)
    except Exception as exc:
        logger.error("Data processing failed: %s", exc)
        sys.exit(2)

    logger.info("=" * 60)
    if summary["skipped"]:
        logger.info("Skipped: Year=%s Month=%s already present in workbook.",
                    summary["year"], summary["month"])
    else:
        logger.info("Appended %d row(s) for Year=%s Month=%s.",
                    summary["rows_appended"], summary["year"], summary["month"])
        logger.info("Backup written alongside the workbook.")
    logger.info("Log saved to: %s", LOG_FILE.resolve())
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
