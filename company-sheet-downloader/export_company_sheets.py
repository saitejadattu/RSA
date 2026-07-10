#!/usr/bin/env python3

import csv
import re
import time
import requests
from pathlib import Path
from datetime import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
DATA_DIR.mkdir(exist_ok=True)

COMPANY_FILE = BASE / "companies.txt"
FAILED_LOG = BASE / "failed_downloads.txt"


def sanitize(text):
    if not text:
        return ""
    return re.sub(r'[\\/:*?"<>|]', "_", text.strip().replace(" ", "_"))


def format_date(date_str):
    if not date_str:
        return "unknown-date"

    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return sanitize(date_str)
def normalize_header(header):
    """Normalize header by removing line breaks and extra spaces."""
    if not header:
        return ""
    return " ".join(header.split()).strip().lower()


def get_value(row, *possible_headers):
    """
    Return the value of the first matching header.
    Header comparison is case-insensitive and ignores line breaks.
    """
    normalized_map = {
        normalize_header(k): (v or "").strip()
        for k, v in row.items()
    }

    for header in possible_headers:
        value = normalized_map.get(normalize_header(header))
        if value:
            return value

    return ""

def get_creds():
    token = BASE / "token.json"
    creds = None

    if token.exists():
        creds = Credentials.from_authorized_user_file(token, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(BASE / "credentials.json"),
                SCOPES,
            )
            creds = flow.run_local_server(port=0)

        token.write_text(creds.to_json())

    return creds


def export_url(url):
    m = re.search(r"/d/([\w-]+)", url)
    g = re.search(r"gid=(\d+)", url)

    if not m:
        return None

    gid = g.group(1) if g else "0"

    return (
        f"https://docs.google.com/spreadsheets/d/"
        f"{m.group(1)}/export?format=tsv&gid={gid}"
    )


def download(session, url, outfile, company, sheet_type):
    export = export_url(url)

    if not export:
        return False, "Invalid URL"

    for attempt in range(3):
        try:
            r = session.get(export, timeout=180)

            if r.status_code == 200:
                outfile.write_text(r.text, encoding="utf-8")
                return True, None

            return False, f"HTTP {r.status_code}"

        except requests.exceptions.ReadTimeout:
            print(
                f"Timeout ({attempt+1}/3) -> {company} ({sheet_type})"
            )
            time.sleep(5)

        except Exception as ex:
            return False, str(ex)

    return False, "Timed out after 3 retries"


def main():

    creds = get_creds()
    creds.refresh(Request())

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {creds.token}"

    success = 0
    failed = 0
    skipped = 0

    failed_entries = []

    with COMPANY_FILE.open(encoding="utf-8-sig") as f:

        reader = csv.DictReader(f, delimiter="\t")

        print("\nDetected Headers (Raw):")
        for h in reader.fieldnames:
            print(repr(h))

        # Normalize headers
        reader.fieldnames = [
            " ".join(h.split()) if h else ""
            for h in reader.fieldnames
        ]

        print("\nDetected Headers (Normalized):")
        for h in reader.fieldnames:
            print(repr(h))

        for row in reader:

            company = get_value(
                row,
                "Company Name"
            )

            if not company:
                continue

            received = get_value(
                row,
                "Opportunity Received On",
                "Opportunity\nReceived On",
            )

            date = format_date(received)

            filename_prefix = f"{date}_{sanitize(company)}"
            print(f"\nProcessing : {company}")
            print(f"Received Date : {received}")
            print(f"Formatted Date: {date}")
            print(f"Filename      : {filename_prefix}")

            print(f"\nProcessing : {company}")

            tasks = [
                (
                    "Student Response Sheet",
                    "responses",
                    get_value(row, "Student Response Sheet"),
                ),
                (
                    "Company Sheet",
                    "shortlists",
                    get_value(row, "Company Sheet"),
                ),
            ]

            for column, suffix, url in tasks:

                if not url:
                    print(f"Skipped {company} {suffix} (No URL)")
                    skipped += 1
                    continue

                outfile = DATA_DIR / f"{filename_prefix}_{suffix}.txt"

                ok, reason = download(
                    session,
                    url,
                    outfile,
                    company,
                    suffix,
                )

                if ok:
                    success += 1
                    print(f"Downloaded {outfile.name}")
                else:
                    failed += 1
                    print(f"Failed {company} {suffix}: {reason}")

                    failed_entries.append(
                        f"{company}\t{received}\t{suffix}\t{reason}"
                    )

                time.sleep(1)

    FAILED_LOG.write_text(
        "\n".join(failed_entries),
        encoding="utf-8",
    )

    print("\n==============================")
    print("Finished")
    print("==============================")
    print("Successful :", success)
    print("Skipped    :", skipped)
    print("Failed     :", failed)
    print("==============================")

    if failed:
        print(f"See {FAILED_LOG.name} for details.")


if __name__ == "__main__":
    main()