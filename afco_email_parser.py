#!/usr/bin/env python3
"""
AFCO Executive Daily Shipments parser.

Reads one or more .eml files (the "AFCO Executive Daily Shipments for
Manufacturing" report) and appends structured rows to a running history
file (JSON). Parses the HTML part of the email (real <table> markup),
which is far more reliable than the padded plain-text columns.

Usage:
    python3 parse_afco_email.py /path/to/folder_of_eml_files --history history.json
    python3 parse_afco_email.py /path/to/one_file.eml --history history.json

Re-running on the same file is safe: rows are keyed by report date, so a
re-parsed day overwrites rather than duplicates.
"""

import argparse
import email
import json
import re
import sys
from email import policy
from pathlib import Path
from datetime import datetime

from bs4 import BeautifulSoup

COLUMNS = [
    "book_today", "ship_today",
    "book_mtd", "ship_mtd",
    "book_ytd", "ship_ytd",
    "open_deliveries",
    "late_credit_blk", "late_other_blk",
    "backlog_total", "backlog_current_month", "backlog_next_month", "backlog_grand_total",
]


def _to_num(tok: str):
    tok = tok.strip().replace(",", "")
    if tok == "" or tok == "&nbsp;":
        return None
    try:
        return int(tok)
    except ValueError:
        try:
            return float(tok)
        except ValueError:
            return None


def _kmto_num(tok: str):
    tok = tok.strip()
    m = re.match(r"^([\d.]+)\s*([KM]?)$", tok)
    if not m:
        return None
    val = float(m.group(1))
    if m.group(2) == "K":
        val *= 1_000
    elif m.group(2) == "M":
        val *= 1_000_000
    return val


def _get_html_body(eml_path: Path) -> str:
    with open(eml_path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_content()
    raise ValueError(f"No text/html part found in {eml_path}")


def parse_report(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    all_tables = soup.find_all("table")
    if len(all_tables) < 2:
        raise ValueError("Expected at least 2 tables (title block + data table)")

    # Identify the tables we need by content, not position — a forwarded copy
    # can prepend a signature block or other tables ahead of the real report.
    title_table, data_table, proj_table = None, None, None
    date_re = re.compile(r"[A-Za-z]{3} \d{1,2}, \d{4}")
    for t in all_tables:
        text = t.get_text(" ", strip=True)
        if data_table is None:
            cells = t.find_all(["td", "th"])
            if any(c.get_text(strip=True) == "Product Group" for c in cells):
                data_table = t
                continue
        if proj_table is None and "Projected Analysis" in text:
            proj_table = t
            continue
        if title_table is None and date_re.search(text) and "Product Group" not in text:
            title_table = t

    if title_table is None:
        raise ValueError("Could not find report date in title block")
    if data_table is None:
        raise ValueError("Could not find the main data table (no 'Product Group' header row)")

    header_text = title_table.get_text(" ", strip=True)
    date_match = date_re.search(header_text)
    report_date = datetime.strptime(date_match.group(0), "%b %d, %Y").date().isoformat()

    # --- Main data table ---
    trs = data_table.find_all("tr")

    # First row: snapshot timestamp
    snap_cell_tag = trs[0].find(["td", "th"])
    snap_cell = snap_cell_tag.get_text(strip=True) if snap_cell_tag else ""
    snap_match = re.match(r"^([A-Za-z]{3} \d{1,2} \d{4} \d{1,2}:\d{2}[AP]M)", snap_cell)
    snapshot_dt = datetime.strptime(snap_match.group(1), "%b %d %Y %I:%M%p").isoformat() if snap_match else None

    rows = {}
    order = []
    # Data rows start at index 2 (index 1 is the "Product Group / Book / Ship..." header row)
    for tr in trs[2:]:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        name = cells[0].get_text(strip=True)
        if not name:
            continue
        vals = [_to_num(c.get_text(strip=True)) for c in cells[1:]]
        if len(vals) != len(COLUMNS):
            continue
        rows[name] = dict(zip(COLUMNS, vals))
        order.append(name)

    # --- Projected Analysis table ---
    projected, fiscal = {}, {}
    if proj_table is not None:
        proj_trs = proj_table.find_all("tr")
        num_pattern = re.compile(r"^[\d.]+\s*[KM]?$")
        numbers_row_texts = None
        fiscal_text = None
        for tr in proj_trs:
            cells = tr.find_all(["td", "th"])
            texts = [c.get_text(strip=True) for c in cells]
            if len(texts) == 4 and all(num_pattern.match(t) for t in texts if t):
                numbers_row_texts = texts
            row_text = tr.get_text(" ", strip=True)
            if "Fiscal" in row_text and "Day" in row_text:
                fiscal_text = row_text
        if numbers_row_texts:
            projected = {
                "month_daily_avg": _kmto_num(numbers_row_texts[0]),
                "month_projected_total": _kmto_num(numbers_row_texts[1]),
                "year_daily_avg": _kmto_num(numbers_row_texts[2]),
                "year_projected_total": _kmto_num(numbers_row_texts[3]),
            }
        if fiscal_text:
            fm = re.findall(r"Day (\d+) out of (\d+) in Fiscal ([A-Za-z]+ \d{4}|\d{4})", fiscal_text)
            if len(fm) == 2:
                fiscal = {
                    "fiscal_month_day": int(fm[0][0]),
                    "fiscal_month_days_total": int(fm[0][1]),
                    "fiscal_month_label": fm[0][2],
                    "fiscal_year_day": int(fm[1][0]),
                    "fiscal_year_days_total": int(fm[1][1]),
                    "fiscal_year_label": fm[1][2],
                }

    return {
        "report_date": report_date,
        "snapshot_datetime": snapshot_dt,
        "product_groups": rows,
        "product_group_order": order,
        "projected_analysis": projected,
        "fiscal_calendar": fiscal,
    }


def load_history(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"reports": {}}


def save_history(history: dict, path: Path):
    with open(path, "w") as f:
        json.dump(history, f, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help=".eml file or folder of .eml files")
    ap.add_argument("--history", default="history.json", help="Path to running history JSON file")
    args = ap.parse_args()

    input_path = Path(args.input)
    history_path = Path(args.history)
    history = load_history(history_path)

    files = sorted(input_path.glob("*.eml")) if input_path.is_dir() else [input_path]
    if not files:
        print("No .eml files found.", file=sys.stderr)
        sys.exit(1)

    added, updated = 0, 0
    for f in files:
        try:
            html = _get_html_body(f)
            report = parse_report(html)
        except Exception as e:
            print(f"SKIP {f.name}: {e}", file=sys.stderr)
            continue
        key = report["report_date"]
        if key in history["reports"]:
            updated += 1
        else:
            added += 1
        history["reports"][key] = report
        print(f"Parsed {f.name} -> {key}  ({len(report['product_groups'])} product groups)")

    save_history(history, history_path)
    print(f"\nDone. {added} new day(s), {updated} re-parsed day(s). "
          f"History now has {len(history['reports'])} day(s) at {history_path}")


if __name__ == "__main__":
    main()
