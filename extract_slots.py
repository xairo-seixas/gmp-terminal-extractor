#!/usr/bin/env python3
"""Extract GMP Terminal de France appointment slots and upload a CSV to Google Drive.
Fixes vs previous version:
- Day column derived from each row's date (was: today's weekday everywhere)
- Junk rows eliminated: a row is only emitted when an hour label is directly
  followed by a real "Capacité / Restants / Attente" data triple
- No timezone-dependent hour math: hours are taken verbatim from the page
- Fails loudly (exit 1) when parsing yields no data or upload verification fails
- Re-uses the existing Drive file for the same filename (update, not duplicate)
- Prints the Drive file ID so the Actions log proves delivery
- Uses Playwright to render JavaScript before parsing (fixes 0-row issue)
"""
import csv
import io
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from playwright.sync_api import sync_playwright

URLS = [
    "https://www.rdvgmp.fr/static/calendar_tdf.html",
    "https://www.rdvgmp.fr/static/calendar_tdf_next_week.html",
]

DAY_HEADER = re.compile(r"Détails des disponibilités\s*du\s*(\d{2}/\d{2}/\d{4})")
HOUR_LABEL = re.compile(r"\b(\d{1,2}):00\s*-\s*(\d{1,2}):00\b")
CAPACITY = re.compile(
    r"Capacité\s*/\s*Restants\s*/\s*Attente\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*%"
)


def fetch_text(url: str) -> str:
    """Fetch a JS-rendered page and reduce its HTML to whitespace-normalized text."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60_000)
        html = page.content()
        browser.close()
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)


def parse_day_section(section: str):
    """Yield (start_hour, capacity, remaining, waitlist_pct) for hours with data.

    Walks hour labels and capacity triples in document order; a capacity triple
    belongs to the closest preceding hour label. Hours without a triple
    (closed slots) produce no row, and stray numbers elsewhere on the page
    can never produce a row because they lack the Capacité prefix.
    """
    events = []
    for m in HOUR_LABEL.finditer(section):
        events.append((m.start(), "hour", (int(m.group(1)), int(m.group(2)))))
    for m in CAPACITY.finditer(section):
        events.append(
            (m.start(), "cap", (int(m.group(1)), int(m.group(2)), int(m.group(3))))
        )
    events.sort(key=lambda e: e[0])

    current_hour = None
    for _, kind, val in events:
        if kind == "hour":
            current_hour = val
        else:  # capacity triple
            if current_hour is not None:
                start, end = current_hour
                if end == start + 1 and 0 <= start <= 23:
                    yield (start, *val)
            current_hour = None


def extract_rows():
    rows = []
    for url in URLS:
        print(f"Fetching {url}")
        text = fetch_text(url)
        parts = DAY_HEADER.split(text)
        # parts = [preamble, date1, body1, date2, body2, ...]
        for i in range(1, len(parts) - 1, 2):
            date_str = parts[i]
            body = parts[i + 1]
            day_name = datetime.strptime(date_str, "%d/%m/%Y").strftime("%A")
            for start, cap, rem, wait in parse_day_section(body):
                fill = round(100 * (cap - rem) / cap) if cap else 0
                rows.append(
                    [
                        date_str,
                        day_name,
                        f"{start}:00 - {start + 1}:00",
                        cap,
                        rem,
                        f"{fill}%",
                        f"{wait}%",
                    ]
                )
            print(f"  {date_str} ({day_name}): "
                  f"{sum(1 for r in rows if r[0] == date_str)} slots with data")
    return rows


def upload_to_drive(filename: str, data: bytes) -> str:
    folder_id = os.environ["GDRIVE_FOLDER_ID"]
    info = json.loads(os.environ["GDRIVE_CREDENTIALS_JSON"])
    if isinstance(info, str):
        info = json.loads(info)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    svc = build("drive", "v3", credentials=creds)
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="text/csv", resumable=False)

    existing = (
        svc.files()
        .list(
            q=f"name = '{filename}' and '{folder_id}' in parents and trashed = false",
            fields="files(id)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
        .get("files", [])
    )

    if existing:
        file_id = existing[0]["id"]
        svc.files().update(
            fileId=file_id, media_body=media, supportsAllDrives=True
        ).execute()
        action = "updated"
    else:
        meta = {"name": filename, "parents": [folder_id]}
        file_id = (
            svc.files()
            .create(body=meta, media_body=media, fields="id", supportsAllDrives=True)
            .execute()["id"]
        )
        action = "created"

    # Round-trip verification: download what Drive stored and compare bytes.
    stored = svc.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
    if stored != data:
        print("FATAL: verification failed — bytes in Drive differ from local bytes.")
        sys.exit(1)

    print(f"OK: {action} {filename} — file id {file_id}, "
          f"{len(data)} bytes, verified byte-identical in Drive.")
    return file_id


def main():
    rows = extract_rows()
    if len(rows) < 10:
        print(f"FATAL: only {len(rows)} slot rows parsed — "
              f"page layout changed or no data rendered. Failing the run.")
        sys.exit(1)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Day", "Time Slot", "Capacity", "Remaining",
                     "Fill %", "Waitlist %"])
    writer.writerows(rows)
    data = buf.getvalue().encode("utf-8")

    today_paris = datetime.now(ZoneInfo("Europe/Paris")).date()
    filename = f"GMP_Terminal_Slots_{today_paris.isoformat()}.csv"
    upload_to_drive(filename, data)
    print(f"Done. {len(rows)} rows written.")


if __name__ == "__main__":
    main()
