import os, re, json, csv
from datetime import date
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

URLS = [
    "https://www.rdvgmp.fr/static/calendar_tdf.html",
    "https://www.rdvgmp.fr/static/calendar_tdf_next_week.html",
]

def parse_page(url):
    from playwright.sync_api import sync_playwright
    print(f"  Fetching {url}...")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        text = page.inner_text("body")
        browser.close()

    print(f"  Text length: {len(text)}")
    # Print 800 chars starting from first date found
    m = re.search(r"\d{2}/\d{2}/2026", text)
    if m:
        print("  SAMPLE:\n" + text[max(0, m.start()-30):m.start()+800])
    return []

if __name__ == "__main__":
    for url in URLS:
        parse_page(url)
        break  # only first URL for now
    print("Done.")
