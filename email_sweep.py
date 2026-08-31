#!/usr/bin/env python3
"""
Daily sweep: connects to a Gmail inbox, finds recent "AFCO Executive Daily
Shipments for Manufacturing" emails, parses them, and merges the results
into history.json.

Designed to run unattended (e.g. via GitHub Actions on a schedule). It is
safe to run every day even if it re-processes the same email more than
once: history.json is keyed by report date, so re-parsing a day just
overwrites it with the same content.

Required environment variables:
    GMAIL_ADDRESS        e.g. railingresearch@gmail.com
    GMAIL_APP_PASSWORD   the 16-character Gmail app password (not the
                          normal account password)

Optional:
    LOOKBACK_DAYS         how many days back to search each run (default 5,
                           enough to cover a long weekend or a missed run)
    HISTORY_PATH           path to history.json (default ./history.json)

Usage:
    GMAIL_ADDRESS=... GMAIL_APP_PASSWORD=... python3 email_sweep.py
"""

import email
import imaplib
import os
import sys
from datetime import datetime, timedelta
from email import policy
from pathlib import Path

from afco_email_parser import parse_report, load_history, save_history

SUBJECT_MATCH = "AFCO Executive Daily Shipments for Manufacturing"
IMAP_HOST = "imap.gmail.com"


def _get_html_body_from_message(msg) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    raise ValueError("No text/html part found in message")


def fetch_recent_messages(address: str, app_password: str, lookback_days: int):
    """Yields parsed email.message.Message objects from the last N days
    whose subject matches the AFCO report, most recent first."""
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(address, app_password)
        imap.select("INBOX")

        since_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        # SUBJECT search is a substring match in Gmail's IMAP implementation
        typ, data = imap.search(None, f'(SINCE "{since_date}" SUBJECT "AFCO Executive Daily Shipments")')
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ} {data}")

        ids = data[0].split()
        if not ids:
            return

        for msg_id in ids:
            typ, msg_data = imap.fetch(msg_id, "(RFC822)")
            if typ != "OK":
                print(f"WARN: could not fetch message {msg_id!r}: {typ}", file=sys.stderr)
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw, policy=policy.default)
            yield msg
    finally:
        try:
            imap.close()
        except Exception:
            pass
        imap.logout()


def main():
    address = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not address or not app_password:
        print("ERROR: GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set.", file=sys.stderr)
        sys.exit(1)

    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "5"))
    history_path = Path(os.environ.get("HISTORY_PATH", "history.json"))

    history = load_history(history_path)
    added, updated, skipped = 0, 0, 0

    for msg in fetch_recent_messages(address, app_password, lookback_days):
        subject = msg.get("Subject", "")
        if SUBJECT_MATCH not in subject:
            skipped += 1
            continue
        try:
            html = _get_html_body_from_message(msg)
            report = parse_report(html)
        except Exception as e:
            print(f"SKIP '{subject}': {e}", file=sys.stderr)
            skipped += 1
            continue

        key = report["report_date"]
        if key in history["reports"]:
            updated += 1
        else:
            added += 1
        history["reports"][key] = report
        print(f"Parsed '{subject}' -> {key}")

    save_history(history, history_path)
    print(f"\nDone. {added} new day(s), {updated} re-parsed day(s), {skipped} skipped. "
          f"History now has {len(history['reports'])} day(s) at {history_path}")


if __name__ == "__main__":
    main()
