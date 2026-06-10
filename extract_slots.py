import os, re, json, csv
from datetime import date
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

URLS = [
    "https://www.rdvgmp.fr/static/calendar_tdf.html",
    "https://www.rdvgmp.fr/static/calendar_tdf_next_week.html",
]

DAY_MAP = {
    'lundi': 'Monday', 'mardi': 'Tuesday', 'mercredi': 'Wednesday',
    'jeudi': 'Thursday', 'vendredi': 'Friday', 'samedi': 'Saturday', 'dimanche': 'Sunday'
}

def parse_page(url):
    from playwright.sync_api import sync_playwright
    print(f"  Fetching {url}...")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        texts = page.evaluate("""
        () => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const texts = [];
            let node;
            while (node = walker.nextNode()) {
                const t = node.textContent.trim();
                if (t) texts.push(t);
            }
            return texts;
        }
        """)
        browser.close()

    print(f"  Text nodes: {len(texts)}")
    slots = []
    current_date = None
    current_day = None
    i = 0
    while i < len(texts):
        t = texts[i].strip()
        date_m = re.search(r'(\d{2}/\d{2}/\d{4})', t)
        if date_m:
            current_date = date_m.group(1)
            i += 1
            continue
        if t.lower() in DAY_MAP:
            current_day = DAY_MAP[t.lower()]
            i += 1
            continue
        time_m = re.match(r'^(\d{1,2}:\d{2})\s*[-\u2013]\s*(\d{1,2}:\d{2})$', t)
        if time_m and current_date:
            start, end = time_m.group(1), time_m.group(2)
            for j in range(i + 1, min(i + 16, len(texts))):
                tj = texts[j].strip()
                cap_m = re.search(r'(\d+)\s*/\s*(\d+)\s*/\s*(\d+)%', tj)
                if cap_m:
                    slots.append({'date': current_date, 'day': current_day or '',
                                  'slot': f"{start} - {end}", 'start': start, 'end': end,
                                  'capacity': int(cap_m.group(1)), 'remaining': int(cap_m.group(2)),
                                  'waitlist_pct': int(cap_m.group(3))})
                    break
                if j + 2 < len(texts):
                    n1 = re.match(r'^(\d+)$', tj)
                    n2 = re.match(r'^(\d+)$', texts[j + 1].strip())
                    n3 = re.match(r'^(\d+)%$', texts[j + 2].strip())
                    if n1 and n2 and n3:
                        slots.append({'date': current_date, 'day': current_day or '',
                                      'slot': f"{start} - {end}", 'start': start, 'end': end,
                                      'capacity': int(n1.group(1)), 'remaining': int(n2.group(1)),
                                      'waitlist_pct': int(n3.group(1))})
                        break
                if re.match(r'^\d{1,2}:\d{2}\s*[-\u2013]', tj):
                    break
        i += 1
    print(f"  Slots found: {len(slots)}")
    return slots


def build_csv(all_slots, output_path):
    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Day", "Time Slot", "Capacity", "Remaining", "Fill %", "Waitlist %"])
        for s in all_slots:
            cap, rem = s["capacity"], s["remaining"]
            w.writerow([s["date"], s["day"], s["slot"], cap, rem,
                        f"{(cap - rem) / cap * 100:.0f}%", f"{s['waitlist_pct']}%"])
    print(f"Saved: {output_path}")


def upload_to_drive(file_path, folder_id, credentials_json, mime_type):
    creds_info = json.loads(credentials_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/drive.file"])
    service = build("drive", "v3", credentials=creds)
    file_name = os.path.basename(file_path)
    existing = service.files().list(
        q=f"name='{file_name}' and '{folder_id}' in parents and trashed=false",
        fields="files(id)").execute().get("files", [])
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
