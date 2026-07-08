"""Fully-automated import: read opportunities from the DB and, for each one, fetch its
Student Response Sheet + Company (shortlist) Sheet straight from Google and import them.

No manual .txt files. The master must already be imported (import_company_master), so each
opportunity has its `student_response_sheet` / `company_sheet` links.

Access: the sheets are private, so pick ONE:
  * make the sheet(s) "anyone with the link can view"  -> run with no --credentials
  * OR a Google service account with the sheet(s) shared to it -> --credentials key.json

    python scripts/import_from_master.py --dry-run                 # public, preview
    python scripts/import_from_master.py --responses-only          # load all response tabs
    python scripts/import_from_master.py --credentials key.json    # service account
    python scripts/import_from_master.py --company "New Age"       # one company only
"""
import argparse
import asyncio
import csv
import io
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx

SCRIPTS = Path(__file__).resolve().parent
sys.path.append(str(SCRIPTS))
sys.path.append(str(SCRIPTS.parent))

import import_company_response as response_importer
import import_company_shortlist as shortlist_importer
from app.db.collections import HIRING_OPPORTUNITIES
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database


class SheetAccessError(Exception):
    pass


def parse_sheet_url(url: str) -> tuple[str, str] | None:
    """Return (spreadsheet_id, gid) from a Google Sheets URL, or None if not parseable."""
    if not url:
        return None
    id_match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if not id_match:
        return None
    gid_match = re.search(r"[?&#]gid=(\d+)", url)
    return id_match.group(1), (gid_match.group(1) if gid_match else "0")


def bearer_token(credentials_path: str | None) -> str | None:
    if not credentials_path:
        return None
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    creds.refresh(Request())
    return creds.token


def fetch_tab_tsv(client: httpx.Client, spreadsheet_id: str, gid: str, token: str | None) -> str:
    """Fetch one tab as tab-separated text via Google's export endpoint."""
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = client.get(url, params={"format": "tsv", "gid": gid}, headers=headers, follow_redirects=True, timeout=30)
    if resp.status_code in (401, 403):
        raise SheetAccessError("no access (private) — share the sheet or use --credentials")
    if resp.status_code == 404:
        raise SheetAccessError("not found (404)")
    if resp.status_code == 429:
        raise SheetAccessError("rate limited (429) — rerun to resume")
    resp.raise_for_status()
    if "text/html" in resp.headers.get("content-type", ""):
        raise SheetAccessError("got a login page, not data — sheet is not accessible")
    return resp.text


def write_temp_tsv(text: str) -> str:
    # Google exports CSV/TSV comma-vs-tab quoting cleanly; write straight through.
    handle = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, encoding="utf-8", newline="")
    handle.write(text)
    handle.close()
    return handle.name


async def process(opportunity: dict, client: httpx.Client, token: str | None,
                  do_responses: bool, do_shortlists: bool, dry_run: bool) -> dict:
    label = f"{opportunity.get('company_name')} / {opportunity.get('role')} ({opportunity.get('opportunity_received_on') or 'no-date'})"
    result: dict = {"label": label}
    common = SimpleNamespace(
        company_name=opportunity.get("company_name"),
        received_on=opportunity.get("opportunity_received_on") or None,
        role=opportunity.get("role") or None,
        dry_run=dry_run,
    )

    if do_responses:
        parsed = parse_sheet_url(opportunity.get("student_response_sheet"))
        if not parsed:
            result["response"] = "no link"
        else:
            temp = None
            try:
                text = fetch_tab_tsv(client, *parsed, token)
                temp = write_temp_tsv(text)
                args = SimpleNamespace(**vars(common), response_sheet=temp)
                res = await response_importer.import_response_sheet(args)
                result["response"] = {"rows": res["rows_read"], "inserted": res["applications_inserted"], "updated": res["applications_updated"]}
            except SheetAccessError as exc:
                result["response"] = f"skip: {exc}"
            except Exception as exc:
                result["response"] = f"error: {exc}"
            finally:
                if temp:
                    Path(temp).unlink(missing_ok=True)

    if do_shortlists:
        parsed = parse_sheet_url(opportunity.get("company_sheet"))
        if not parsed:
            result["shortlist"] = "no link"
        else:
            temp = None
            try:
                text = fetch_tab_tsv(client, *parsed, token)
                temp = write_temp_tsv(text)
                args = SimpleNamespace(**vars(common), shortlist_sheet=temp)
                res = await shortlist_importer.import_shortlist(args)
                result["shortlist"] = {"rows": res["rows_read"], "marked": res["applications_marked_shortlisted"], "unmatched": res["unmatched"]}
            except SheetAccessError as exc:
                result["shortlist"] = f"skip: {exc}"
            except Exception as exc:
                result["shortlist"] = f"error: {exc}"
            finally:
                if temp:
                    Path(temp).unlink(missing_ok=True)

    return result


async def run(args: argparse.Namespace) -> dict:
    await connect_to_mongo()
    db = get_database()

    query: dict = {}
    if args.company:
        query["company_name"] = {"$regex": re.escape(args.company), "$options": "i"}
    opportunities = await db[HIRING_OPPORTUNITIES].find(query).to_list(length=None)
    if args.limit:
        opportunities = opportunities[: args.limit]

    token = bearer_token(args.credentials)
    do_responses = not args.shortlists_only
    do_shortlists = not args.responses_only

    results = []
    with httpx.Client() as client:
        for opportunity in opportunities:
            results.append(await process(opportunity, client, token, do_responses, do_shortlists, args.dry_run))

    await close_mongo_connection()

    def loaded(key: str) -> int:
        return sum(1 for r in results if isinstance(r.get(key), dict))

    def skipped(key: str) -> int:
        return sum(1 for r in results if isinstance(r.get(key), str) and r[key].startswith("skip"))

    return {
        "mode": "dry_run" if args.dry_run else "apply",
        "opportunities_scanned": len(opportunities),
        "responses_loaded": loaded("response"),
        "responses_skipped_no_access": skipped("response"),
        "shortlists_loaded": loaded("shortlist"),
        "shortlists_skipped_no_access": skipped("shortlist"),
        "details": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto-fetch each opportunity's response + shortlist sheet from Google and import.")
    parser.add_argument("--credentials", default=None, help="Service-account JSON key (omit to use public link access).")
    parser.add_argument("--company", default=None, help="Only opportunities whose company name matches this.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N opportunities (for testing).")
    parser.add_argument("--responses-only", action="store_true")
    parser.add_argument("--shortlists-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Fetch + validate, write nothing.")
    return parser.parse_args()


if __name__ == "__main__":
    import json

    print(json.dumps(asyncio.run(run(parse_args())), default=str, indent=2))
