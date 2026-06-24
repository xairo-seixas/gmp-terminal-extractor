#!/usr/bin/env python3
"""Extract GMP terminal appointment slots and upload CSVs + screenshots to Google Drive.

Supports multiple terminals. Each terminal is processed independently — a failure
in one does not abort the others. The run exits 1 only if at least one terminal
failed, so the Actions log makes clear which ones succeeded and which did not.
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

# ---------------------------------------------------------------------------
# Terminal configuration — add new terminals here, nothing else needs changing
# ---------------------------------------------------------------------------
TERMINALS = [
    {
        "slug": "tdf",
        "label": "Terminal de France (Le Havre)",
        "urls": [
            "https://www.rdvgmp.fr/static/calendar_tdf.html",
            "https://www.rdvgmp.fr/static/calendar_tdf_next_week.html",
        ],
        "min_rows": 10,
    },
    # Example — uncomment and fill in to add another terminal:
    # {
    #     "slug": "other",
    #     "label": "Other Terminal",
    #     "urls": [
    #         "https://www.rdvgmp.fr/static/calendar_other.html",
    #     ],
    #     "min_rows": 5,
    # },
]

DAY_HEADER = re.compile(r"Détails des disponibilités\s*du\s*(\d{2}/\d{2}/\d{4})")
HOUR_LABEL = re.compile(r"\b(\d{1,2}):00\s*-\s*(\d{1,2}):00\b")
CAPACITY = re.compile(
    r"Capacité\s*/\s*Restants\s*/\s*Attente\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*%"
)


# ---------------------------------------------------------------------------
# Fetching & parsing
# ---------------------------------------------------------------------------

def fetch_text_and_screenshot(url: str, browser) -> tuple[str, bytes]:
    """Render a JS page with an existing browser instance; return (text, png_bytes)."""
    page = browser.new_page()
    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
        html = page.content()
        png_bytes = page.screenshot(full_page=True)
    finally:
        page.close()
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text), png_bytes


def parse_day_section(section: str):
    """Yield (start_hour, capacity, remaining, waitlist_pct) for hours with data."""
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
        else:
            if current_hour is not None:
                start, end = current_hour
                if end == start + 1 and 0 <= start <= 23:
                    yield (start, *val)
            current_hour = None


# ---------------------------------------------------------------------------
# Per-terminal processing — raises on any failure so main() can catch it
# ---------------------------------------------------------------------------

def process_terminal(terminal: dict, browser, svc: object, folder_id: str,
                     today_str: str) -> int:
    """Fetch, parse, and upload one terminal. Returns number of rows uploaded.

    Raises RuntimeError with a descriptive message on any failure so the caller
    can log it and move on to the next terminal.
    """
    slug = terminal["slug"]
    label = terminal["label"]
    min_rows = terminal["min_rows"]
    print(f"\n── {label} ({slug}) ──")

    rows = []
    screenshots = []

    for url in terminal["urls"]:
        print(f"  Fetching {url}")
        text, png_bytes = fetch_text_and_screenshot(url, browser)

        url_slug = "next_week" if "next_week" in url else "current_week"
        screenshot_name = f"GMP_Terminal_Screenshot_{today_str}_{slug}_{url_slug}.png"
        screenshots.append((screenshot_name, png_bytes))
        print(f"  Screenshot captured: {screenshot_name} ({len(png_bytes):,} bytes)")

        parts = DAY_HEADER.split(text)
        for i in range(1, len(parts) - 1, 2):
            date_str = parts[i]
            body = parts[i + 1]
            day_name = datetime.strptime(date_str, "%d/%m/%Y").strftime("%A")
            for start, cap, rem, wait in parse_day_section(body):
                fill = round(100 * (cap - rem) / cap) if cap else 0
                rows.append([date_str, day_name, f"{start}:00 - {start + 1}:00",
                              cap, rem, f"{fill}%", f"{wait}%"])
            slot_count = sum(1 for r in rows if r[0] == date_str)
            print(f"  {date_str} ({day_name}): {slot_count} slots with data")

    if len(rows) < min_rows:
        raise RuntimeError(
            f"Only {len(rows)} rows parsed (minimum {min_rows}) — "
            f"page layout may have changed."
        )

    # Build and upload CSV
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Day", "Time Slot", "Capacity", "Remaining",
                     "Fill %", "Waitlist %"])
    writer.writerows(rows)
    csv_data = buf.getvalue().encode("utf-8")
    csv_filename = f"GMP_Terminal_Slots_{today_str}_{slug}.csv"
    upload_file(csv_filename, csv_data, "text/csv", svc, folder_id, verify=True)

    # Upload screenshots
    for name, png_bytes in screenshots:
        upload_file(name, png_bytes, "image/png", svc, folder_id, verify=False)

    print(f"  ✓ {slug}: {len(rows)} rows + {len(screenshots)} screenshot(s) uploaded.")
    return len(rows)


# ---------------------------------------------------------------------------
# Drive upload helper
# ---------------------------------------------------------------------------

def upload_file(filename: str, data: bytes, mimetype: str, svc, folder_id: str,
                verify: bool) -> str:
    """Upload or update a file in Drive; optionally verify byte-identical round-trip."""
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)

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

    if verify:
        stored = svc.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        if stored != data:
            raise RuntimeError(f"Verification failed for {filename} — bytes differ in Drive.")

    print(f"  {action} {filename} — {file_id} ({len(data):,} bytes)")
    return file_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    today_str = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()

    folder_id = os.environ["GDRIVE_FOLDER_ID"]
    info = json.loads(os.environ["GDRIVE_CREDENTIALS_JSON"])
    if isinstance(info, str):
        info = json.loads(info)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    svc = build("drive", "v3", credentials=creds)

    failures = []

    # Single browser instance shared across all terminals
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for terminal in TERMINALS:
                try:
                    process_terminal(terminal, browser, svc, folder_id, today_str)
                except Exception as exc:
                    msg = f"{terminal['slug']}: {exc}"
                    print(f"\n  ✗ FAILED — {msg}")
                    failures.append(msg)
        finally:
            browser.close()

    print(f"\n── Summary ──")
    print(f"  Terminals processed : {len(TERMINALS)}")
    print(f"  Succeeded           : {len(TERMINALS) - len(failures)}")
    print(f"  Failed              : {len(failures)}")

    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  • {f}")
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
