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

def _try_cap(texts, j):
    tj = texts[j].strip()
    # Pattern 1: "85 / 6 / 0%" in one node
    cap_m = re.search(r'(\d+)\s*/\s*(\d+)\s*/\s*(\d+)%?', tj)
    if cap_m:
        return {'capacity': int(cap_m.group(1)), 'remaining': int(cap_m.group(2)),
                'waitlist_pct': int(cap_m.group(3))}
    # Pattern 2: "80" + "/\n 49 / \n 0%" (actual format seen in logs)
    n1 = re.match(r'^(\d+)$', tj)
    if n1 and j + 1 < len(texts):
        rest = texts[j + 1].strip()
        rest_m = re.search(r'/\s*(\d+)\s*/\s*(\d+)%?', rest)
        if rest_m:
            return {'capacity': int(n1.group(1)), 'remaining': int(rest_m.group(1)),
                    'waitlist_pct': int(rest_m.group(2))}
    # Pattern 3: "85", "/", "6", "/", "0%" as 5 nodes
    if j + 4 < len(texts):
        n1 = re.match(r'^(\d+)$', tj)
        s1 = texts[j+1].strip() in ['/', '|']
        n2 = re.match(r'^(\d+)$', texts[j+2].strip()) if s1 else None
        s2 = texts[j+3].strip() in ['/', '|'] if n2 else None
        n3 = re.match(r'^(\d+)%?$', texts[j+4].strip()) if s2 else None
        if n1 and s1 and n2 and s2 and n3:
            return {'capacity': int(n1.group(1)), 'remaining': int(n2.group(1)),
                    'waitlist_pct': int(n3.group(1).rstrip('%'))}
    # Pattern 4: "85", "6", "0%" as 3 nodes
    if j + 2 < len(texts):
        n2 = re.match(r'^(\d+)$', texts[j+1].strip()) if n1 else None
        n3 = re.match(r'^(\d+)%?$', texts[j+2].strip()) if n2 else None
        if n1 and n2 and n3:
            return {'capacity': int(n1.group(1)), 'remaining': int(n2.group(1)),
                    'waitlist_pct': int(n3.group(1).rstrip('%'))}
    return None

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
        time_m = re.search(r'(\d{1,2}:\d{2})\s*[-\u2013\u2014]\s*(\d{1,2}:\d{2})', t)
        if time_m and current_date:
            start, end = time_m.group(1), time_m.group(2)
            for j in range(i + 1, min(i + 20, len(texts))):
                found = _try_cap(texts, j)
                if found:
                    slots.append({'date': current_date, 'day': current_day or '',
                                  'slot': f"{start} - {end}", 'start': start, 'end': end,
                                  **found})
                    break
                if j > i + 1 and re.match(r'^\d{1,2}:\d{2}', texts[j].strip()):
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
                        f"{(cap-rem)/cap*100:.0f}%", f"{s['waitlist_pct']}%"])
    print(f"Saved: {output_path}")

def upload_to_drive(file_path, folder_id, credentials_json, mime_type):
    creds_info = json.loads(credentials_json)
    if isinstance(creds_info, str):
        creds_info = json.loads(creds_info)
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
