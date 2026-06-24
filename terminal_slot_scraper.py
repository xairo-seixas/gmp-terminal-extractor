#!/usr/bin/env python3
"""Extract terminal slot availability and upload CSVs + screenshots to Google Drive.

Supported terminals:
  - TDF type   : Terminal de France (Le Havre) — JS-rendered, regex-parsed text
  - GCT type   : GCT Deltaport / Vanterm (Vancouver) — server-rendered HTML table
  - TruckGate  : Hamburg TruckGate — React SPA, one day at a time

Each terminal is processed independently. A failure in one does not abort others.
The run exits 1 only if at least one terminal failed.
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
# Terminal configuration — add new terminals here only
# ---------------------------------------------------------------------------
TERMINALS = [
    {
        "slug": "tdf",
        "label": "Terminal de France (Le Havre)",
        "type": "tdf",
        "urls": [
            "https://www.rdvgmp.fr/static/calendar_tdf.html",
            "https://www.rdvgmp.fr/static/calendar_tdf_next_week.html",
        ],
        "min_rows": 10,
    },
    {
        "slug": "gct_deltaport",
        "label": "GCT Deltaport (Vancouver)",
        "type": "gct",
        "url": "https://webservices.globalterminals.com/tsiWebServiceClient/ReservationAvailabilityStatus.jsp?terminal=DELTAPORT",
        "min_rows": 5,
    },
    {
        "slug": "gct_vanterm",
        "label": "GCT Vanterm (Vancouver)",
        "type": "gct",
        "url": "https://webservices.globalterminals.com/tsiWebServiceClient/ReservationAvailabilityStatus.jsp?terminal=VANTERM",
        "min_rows": 5,
    },
    {
        "slug": "truckgate_hamburg",
        "label": "Hamburg TruckGate",
        "type": "truckgate",
        "url": "https://slot.truckgate.de/slots/",
        # Only extract these sub-terminals; empty list = all
        "terminals_filter": [
            "Eurogate CTH", "Eurogate EKOM", "EUROGATE CTB", "EUROGATE CTW",
            "HHLA CTA", "HHLA CTB", "HHLA CTT",
        ],
        "min_rows": 10,
    },
]

# Known GCT column order (server-rendered JSP, stable format)
GCT_HEADERS = [
    "Date", "Period",
    "Empty In", "Empty Out", "Full In", "Full Out", "Reefer In",
    "AE", "AW", "BE", "BW", "CE", "CW", "DE", "DW",
    "EW", "FW", "IT", "IW", "IZ", "JT", "JZ",
    "KT", "KZ", "LE", "LZ", "MW", "NW",
]

# TruckGate background-color → human-readable status
TRUCKGATE_STATUS = {
    "lightgrey":         "Closed",
    "lightgreen":        "Available",
    "gold":              "Near Full",
    "rgb(255, 110, 84)": "Full",
    "lightblue":         "Other System",
}

# TDF regexes
DAY_HEADER = re.compile(r"Détails des disponibilités\s*du\s*(\d{2}/\d{2}/\d{4})")
HOUR_LABEL = re.compile(r"\b(\d{1,2}):00\s*-\s*(\d{1,2}):00\b")
CAPACITY   = re.compile(
    r"Capacité\s*/\s*Restants\s*/\s*Attente\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*%"
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def open_page(url: str, browser, extra_wait_ms: int = 0):
    """Navigate to a URL; return (page, png_bytes). Caller must close page."""
    page = browser.new_page()
    page.goto(url, wait_until="networkidle", timeout=60_000)
    if extra_wait_ms:
        page.wait_for_timeout(extra_wait_ms)
    png_bytes = page.screenshot(full_page=True)
    return page, png_bytes


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
    print(f"    {action}: {filename} ({len(data):,} bytes)")
    return file_id


# ---------------------------------------------------------------------------
# TDF parser (Le Havre)
# ---------------------------------------------------------------------------

def _parse_tdf_section(section: str):
    """Yield (start_hour, capacity, remaining, waitlist_pct) for a TDF day section."""
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


def process_tdf(terminal: dict, browser, today_str: str):
    """Returns (headers, rows, screenshots_list)."""
    headers = ["Date", "Day", "Time Slot", "Capacity", "Remaining", "Fill %", "Waitlist %"]
    rows = []
    screenshots = []

    for url in terminal["urls"]:
        print(f"    Fetching {url}")
        page, png_bytes = open_page(url, browser)
        try:
            html = page.content()
        finally:
            page.close()

        slug = "next_week" if "next_week" in url else "current_week"
        screenshots.append((
            f"GMP_Terminal_Screenshot_{today_str}_{terminal['slug']}_{slug}.png",
            png_bytes,
        ))

        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)

        parts = DAY_HEADER.split(text)
        for i in range(1, len(parts) - 1, 2):
            date_str = parts[i]
            body = parts[i + 1]
            day_name = datetime.strptime(date_str, "%d/%m/%Y").strftime("%A")
            for start, cap, rem, wait in _parse_tdf_section(body):
                fill = round(100 * (cap - rem) / cap) if cap else 0
                rows.append([date_str, day_name, f"{start}:00 - {start + 1}:00",
                              cap, rem, f"{fill}%", f"{wait}%"])
            slot_count = sum(1 for r in rows if r[0] == date_str)
            print(f"    {date_str} ({day_name}): {slot_count} slots")

    return headers, rows, screenshots


# ---------------------------------------------------------------------------
# GCT parser (Vancouver — server-rendered HTML table)
# ---------------------------------------------------------------------------

def process_gct(terminal: dict, browser, today_str: str):
    """Returns (headers, rows, screenshots_list)."""
    url = terminal["url"]
    print(f"    Fetching {url}")
    page, png_bytes = open_page(url, browser)
    try:
        table_data = page.evaluate("""
        () => {
            const table = document.querySelector('table');
            if (!table) return [];
            return Array.from(table.querySelectorAll('tr')).map(tr =>
                Array.from(tr.querySelectorAll('th, td')).map(cell => {
                    const img = cell.querySelector('img');
                    return img
                        ? (img.getAttribute('title') || img.getAttribute('alt') || '')
                        : cell.innerText.trim();
                })
            );
        }
        """)
    finally:
        page.close()

    screenshots = [(
        f"GMP_Terminal_Screenshot_{today_str}_{terminal['slug']}.png",
        png_bytes,
    )]

    if not table_data or len(table_data) < 3:
        raise RuntimeError("GCT table not found or fewer than 3 rows returned")

    # Rows 0–1 are double-labeled headers; data starts at row 2
    data_rows = [row for row in table_data[2:] if any(cell.strip() for cell in row)]

    # Align each row to the known GCT_HEADERS length
    n = len(GCT_HEADERS)
    rows = [row[:n] + [""] * max(0, n - len(row)) for row in data_rows]

    print(f"    {len(rows)} time slots parsed")
    return GCT_HEADERS, rows, screenshots


# ---------------------------------------------------------------------------
# TruckGate parser (Hamburg — React SPA, single day only)
# ---------------------------------------------------------------------------

def process_truckgate(terminal: dict, browser, today_str: str):
    """Returns (headers, rows, screenshots_list).

    Note: TruckGate only exposes the current day — no multi-day navigation.
    """
    url = terminal["url"]
    tf  = terminal.get("terminals_filter", [])
    print(f"    Fetching {url}")

    page, png_bytes = open_page(url, browser, extra_wait_ms=4000)
    try:
        date_text  = page.inner_text("span.titlebar span")
        date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", date_text)
        page_date  = date_match.group(1) if date_match else today_str

        slot_data = page.evaluate(
            """
            (terminalsFilter) => {
                const blocks = Array.from(
                    document.querySelectorAll('div.SlotGrid-TermBlock')
                );
                const rows = [];
                for (const block of blocks) {
                    const nameEl = block.querySelector('div.SlotGrid-TermHeader1 a');
                    if (!nameEl) continue;
                    const termName = nameEl.innerText.trim();
                    if (terminalsFilter.length > 0 &&
                        !terminalsFilter.includes(termName)) continue;

                    const cells = Array.from(
                        block.querySelectorAll('div.SlotGrid-Cell')
                    );
                    for (const cell of cells) {
                        const style   = cell.getAttribute('style') || '';
                        const bgMatch = style.match(/background-color:\\s*([^;]+)/);
                        const bgColor = bgMatch ? bgMatch[1].trim() : '';

                        const hourEl = cell.querySelector(
                            'div[style*="font-size: 75%"]'
                        );
                        const hour = hourEl ? hourEl.innerText.trim() : '';

                        const pb     = cell.querySelector('[role="progressbar"]');
                        const fillPct = pb ? pb.getAttribute('aria-valuenow') : '';

                        rows.push([termName, hour, bgColor, fillPct]);
                    }
                }
                return rows;
            }
            """,
            tf,
        )
    finally:
        page.close()

    screenshots = [(
        f"GMP_Terminal_Screenshot_{today_str}_{terminal['slug']}.png",
        png_bytes,
    )]

    headers = ["Date", "Terminal", "Hour", "Status", "Fill %"]
    rows = []
    for term_name, hour, bg_color, fill_pct in slot_data:
        status = TRUCKGATE_STATUS.get(bg_color, "Unknown")
        if status == "Closed":
            continue  # skip outside-hours slots
        fill = f"{fill_pct}%" if fill_pct else ""
        rows.append([page_date, term_name, f"{hour}:00", status, fill])

    print(f"    Date: {page_date} — {len(rows)} open slots across "
          f"{len(set(r[1] for r in rows))} terminals")
    return headers, rows, screenshots


# ---------------------------------------------------------------------------
# Terminal dispatcher
# ---------------------------------------------------------------------------

def process_terminal(terminal: dict, browser, svc, folder_id: str, today_str: str) -> int:
    """Process one terminal end-to-end. Raises RuntimeError on any failure."""
    slug  = terminal["slug"]
    label = terminal["label"]
    ttype = terminal.get("type", "tdf")
    print(f"\n── {label} ({slug}) ──")

    if ttype == "tdf":
        headers, rows, screenshots = process_tdf(terminal, browser, today_str)
    elif ttype == "gct":
        headers, rows, screenshots = process_gct(terminal, browser, today_str)
    elif ttype == "truckgate":
        headers, rows, screenshots = process_truckgate(terminal, browser, today_str)
    else:
        raise RuntimeError(f"Unknown terminal type: {ttype!r}")

    if len(rows) < terminal.get("min_rows", 1):
        raise RuntimeError(
            f"Only {len(rows)} rows parsed (minimum {terminal['min_rows']}) — "
            "page layout may have changed."
        )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    csv_data = buf.getvalue().encode("utf-8")

    upload_file(
        f"GMP_Terminal_Slots_{today_str}_{slug}.csv",
        csv_data, "text/csv", svc, folder_id, verify=True,
    )
    for name, png_bytes in screenshots:
        upload_file(name, png_bytes, "image/png", svc, folder_id, verify=False)

    print(f"  ✓ {slug}: {len(rows)} rows + {len(screenshots)} screenshot(s) uploaded.")
    return len(rows)


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
    print(f"  Terminals : {len(TERMINALS)}")
    print(f"  Succeeded : {len(TERMINALS) - len(failures)}")
    print(f"  Failed    : {len(failures)}")

    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  • {f}")
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
