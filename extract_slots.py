import os
import re
import json
import csv
from datetime import date
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

URLS = [
    "https://www.rdvgmp.fr/static/calendar_tdf.html",
    "https://www.rdvgmp.fr/static/calendar_tdf_next_week.html",
]

def parse_page(url):
    from playwright.sync_api import sync_playwright
    print(f"  Launching Playwright for {url}...")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        html = page.content()
        browser.close()

    print(f"  HTML length: {len(html)} chars")
    soup = BeautifulSoup(html, "html.parser")
    h2s = soup.find_all("h2")
    print(f"  Found {len(h2s)} h2 tags")

    slots = []
    days = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    day_names_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    total_rdv = None
    for tag in soup.find_all(string=re.compile(r"Nb de RDV disponibles")):
        m = re.search(r":\s*([\d,\s]+)", tag)
        if m:
            total_rdv = int(re.sub(r"[^\d]", "", m.group(1)))

    cancellations = {}
    for section in soup.find_all("div", class_=re.compile(r"day|date", re.I)):
        date_tag = section.find(string=re.compile(r"\d{2}/\d{2}/\d{4}"))
        canc_tag = section.find(string=re.compile(r"Annulations Transporteur"))
        if date_tag and canc_tag:
            nxt = canc_tag.find_next(string=re.compile(r"\d+"))
            if nxt:
                m = re.search(r"(\d+)", nxt)
                if m:
                    cancellations[date_tag.strip()] = int(m.group(1))

    for header in h2s:
        text = header.get_text()
        m = re.search(r"(\d{2}/\d{2}/\d{4})", text)
        if not m:
            continue
        slot_date = m.group(1)
        print(f"  Parsing date: {slot_date}")

        day_en = ""
        prev = header.find_previous(string=re.compile("|".join(days), re.I))
        if prev:
            for fr, en in zip(days, day_names_en):
                if fr in prev.lower():
                    day_en = en
                    break

        for sibling in header.find_next_siblings():
            time_m = re.match(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", sibling.get_text())
            if not time_m:
                continue
            start, end = time_m.group(1), time_m.group(2)
            full_text = sibling.get_text(" ", strip=True)
            cap_m = re.search(r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)%", full_text)
            if cap_m:
                cap, rem, wait = int(cap_m.group(1)), int(cap_m.group(2)), int(cap_m.group(3))
                slots.append({
                    "date": slot_date, "day": day_en,
                    "slot": f"{start} - {end}", "start": start, "end": end,
                    "capacity": cap, "remaining": rem, "waitlist_pct": wait,
                    "cancellations": cancellations.get(slot_date),
                    "total_rdv": total_rdv,
                })

    print(f"  Slots found: {len(slots)}")
    return slots


def build_csv(all_slots, output_path):
    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Day", "Time Slot", "Capacity", "Remaining", "Fill %", "Waitlist %"])
        for s in all_slots:
            cap, rem = s["capacity"], s["remaining"]
            fill = f"{(cap-rem)/cap*100:.0f}%"
            w.writerow([s["date"], s["day"], s["slot"], cap, rem, fill, f"{s['waitlist_pct']}%"])
    print(f"Saved CSV: {output_path}")


def upload_to_drive(file_path, folder_id, credentials_json, mime_type):
    creds_info = json.loads(credentials_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/drive.file"])
    service = build("drive", "v3", credentials=creds)
    file_name = os.path.basename(file_path)
    results = service.files().list(
        q=f"name='{file_name}' and '{folder_id}' in parents and trashed=false",
        fields="files(id)").execute()
    existing = results.get("files", [])
    media = MediaFileUpload(file_path, mimetype=mime_type)
    if existing:
        service.files().update(fileId=existing[0]["id"], media_body=media).execute()
        print(f"Updated: {file_name}")
    else:
        service.files().create(body={"name": file_name, "parents": [folder_id]},
                               media_body=media, fields="id").execute()
        print(f"Uploaded: {file_name}")


if __name__ == "__main__":
    print("Fetching GMP calendar...")
    all_slots = []
    for url in URLS:
        try:
            slots = parse_page(url)
            all_slots.extend(slots)
            print(f"  {url}: {len(slots)} slots")
        except Exception as e:
            print(f"  WARNING: {url} failed: {e}")

    if not all_slots:
        raise RuntimeError("No slots extracted — aborting.")

    today = date.today().strftime("%Y-%m-%d")
    csv_name = f"GMP_Terminal_Slots_{today}.csv"
    build_csv(all_slots, csv_name)

    folder_id = os.environ.get("GDRIVE_FOLDER_ID", "1x8zXb7p9oiSB1C_38eCIX72oU4fLH91L")
    credentials_json = os.environ["GDRIVE_CREDENTIALS_JSON"]
    upload_to_drive(csv_name, folder_id, credentials_json, mime_type="text/csv")
    print("Done.")
