"""
data_processor.py
-----------------
Cleans the Cockpit CSV and appends the new month into the historical
workbook (Volume Cockpit.xlsx) that lives in a synced SharePoint folder.

Pipeline:
  1. Read the Tableau Crosstab CSV.
  2. Rename columns: PRD_STR→Month, Clstr Show→Clstr Good,
     BU Show→BU Good, MU Show→MU Good.
  3. Filter to (current year, previous fiscal-period month label).
     Cockpit's period labels are P2/Jan .. P13/Dec — running on the
     first days of September means we keep P9/Aug.
  4. If those rows are already in the workbook, skip (dedup).
  5. Otherwise copy the workbook → Volume Cockpit Backup.xlsx, then
     append the new rows to Volume Cockpit.xlsx in place.
"""

from __future__ import annotations

import logging
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
from openpyxl import load_workbook

logger = logging.getLogger(__name__)


COLUMN_RENAMES = {
    "PRD_STR": "Month",
    "Clstr Show": "Clstr Good",
    "BU Show": "BU Good",
    "MU Show": "MU Good",
}


def previous_period(today: date) -> Tuple[int, str]:
    """
    (year, month_label) for the fiscal month just ended.

    Cockpit's periods run P2/Jan .. P13/Dec — the label for calendar
    month N is P{N+1}/{MonthAbbr}. Running on 2026-09-04 returns
    (2026, "P9/Aug"); running on 2027-01-05 returns (2026, "P13/Dec").
    """
    first_of_this_month = today.replace(day=1)
    last_of_prev = first_of_this_month - timedelta(days=1)
    period_num = last_of_prev.month + 1  # Jan=P2, Feb=P3, …, Dec=P13
    abbr = last_of_prev.strftime("%b")
    return last_of_prev.year, f"P{period_num}/{abbr}"


def load_and_clean(csv_path: Path) -> pd.DataFrame:
    """Read the Tableau Crosstab CSV and apply the column renames."""
    # Tableau's Crosstab export is UTF-16-LE with a tab delimiter on
    # most systems; some tenants ship UTF-8. `sep=None` + engine=python
    # lets pandas sniff the delimiter (tab vs comma).
    last_err: Optional[Exception] = None
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            df = pd.read_csv(csv_path, sep=None, engine="python", encoding=encoding)
            logger.info("CSV read with encoding=%s (%d rows, %d cols).",
                        encoding, len(df), len(df.columns))
            break
        except (UnicodeError, UnicodeDecodeError) as exc:
            last_err = exc
    else:
        raise RuntimeError(f"Could not decode {csv_path}: {last_err}")

    missing = [src for src in COLUMN_RENAMES if src not in df.columns]
    if missing:
        raise KeyError(
            f"Expected columns not in CSV: {missing}. "
            f"Actual columns: {list(df.columns)}"
        )
    return df.rename(columns=COLUMN_RENAMES)


def filter_to_period(df: pd.DataFrame, year: int, month_label: str) -> pd.DataFrame:
    """Keep only rows whose Year + Month match the target period."""
    for col in ("Year", "Month"):
        if col not in df.columns:
            raise KeyError(f"Column '{col}' missing after rename. "
                           f"Have: {list(df.columns)}")
    mask = (
        df["Year"].astype(str).str.strip() == str(year)
    ) & (
        df["Month"].astype(str).str.strip() == month_label
    )
    filtered = df[mask].copy()
    logger.info("Filtered to Year=%s Month=%s → %d row(s).",
                year, month_label, len(filtered))
    return filtered


def period_already_present(workbook_path: Path, year: int, month_label: str,
                           sheet_name: str = "") -> bool:
    """True if a row for (year, month_label) already exists in the sheet."""
    wb = load_workbook(workbook_path, read_only=True)
    try:
        ws = wb[sheet_name] if sheet_name else wb.active
        headers = {cell.value: idx for idx, cell in enumerate(ws[1], start=1)
                   if cell.value is not None}
        year_col = headers.get("Year")
        month_col = headers.get("Month")
        if year_col is None or month_col is None:
            logger.warning("Workbook missing 'Year' or 'Month' header — "
                           "dedup check skipped.")
            return False
        year_str = str(year)
        for row in ws.iter_rows(min_row=2, values_only=True):
            y = row[year_col - 1]
            m = row[month_col - 1]
            if y is not None and m is not None \
                    and str(y).strip() == year_str \
                    and str(m).strip() == month_label:
                return True
        return False
    finally:
        wb.close()


def append_to_workbook(rows: pd.DataFrame, workbook_path: Path,
                       backup_path: Path, sheet_name: str = "") -> int:
    """
    Back up the workbook, then append `rows` in place. Preserves the
    workbook's other sheets/formatting via openpyxl. Cells are placed
    by header name — extra CSV columns are logged and skipped.
    """
    shutil.copy2(workbook_path, backup_path)
    logger.info("Backed up workbook → %s", backup_path)

    wb = load_workbook(workbook_path)
    ws = wb[sheet_name] if sheet_name else wb.active
    logger.info("Appending to sheet: %s", ws.title)

    headers = {cell.value: idx for idx, cell in enumerate(ws[1], start=1)
               if cell.value is not None}
    unknown = [c for c in rows.columns if c not in headers]
    if unknown:
        logger.warning("CSV columns not in workbook (skipped): %s", unknown)

    appended = 0
    for _, row in rows.iterrows():
        target_row = ws.max_row + 1
        for col_name, value in row.items():
            col_idx = headers.get(col_name)
            if col_idx is None:
                continue
            if pd.isna(value):
                value = None
            ws.cell(row=target_row, column=col_idx, value=value)
        appended += 1

    wb.save(workbook_path)
    logger.info("Appended %d row(s) → %s", appended, workbook_path)
    return appended


def process(csv_path: Path, cfg: dict, today: Optional[date] = None) -> dict:
    """
    End-to-end: read CSV, rename, filter, dedup, back up, append.
    Returns a summary dict for logging.
    """
    excel_cfg = cfg["excel"]
    folder = Path(excel_cfg["folder"]).expanduser()
    workbook_path = folder / excel_cfg["file_name"]
    backup_path = folder / excel_cfg["backup_name"]
    sheet_name = (excel_cfg.get("sheet_name") or "").strip()

    if not workbook_path.exists():
        raise FileNotFoundError(
            f"Workbook not found: {workbook_path}. Check `excel.folder` and "
            f"`excel.file_name` in config.local.yaml, and confirm the "
            f"SharePoint folder is synced to that path."
        )

    year, month_label = previous_period(today or date.today())
    logger.info("Target period: Year=%s Month=%s", year, month_label)

    df = load_and_clean(csv_path)
    slice_ = filter_to_period(df, year, month_label)
    if slice_.empty:
        raise RuntimeError(
            f"No CSV rows match Year={year} Month={month_label!r}. Verify the "
            f"Tableau view actually contains that period."
        )

    if period_already_present(workbook_path, year, month_label, sheet_name):
        logger.warning("Rows for Year=%s Month=%s already in workbook — "
                       "skipping append (delete them from Excel first if "
                       "you actually want to re-run this period).",
                       year, month_label)
        return {"year": year, "month": month_label, "rows_appended": 0,
                "backup_created": False, "skipped": True}

    appended = append_to_workbook(slice_, workbook_path, backup_path, sheet_name)
    return {"year": year, "month": month_label, "rows_appended": appended,
            "backup_created": True, "skipped": False}
