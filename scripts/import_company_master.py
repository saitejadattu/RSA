import argparse
import asyncio
import csv
import re
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import COMPANIES
from app.db.indexes import create_indexes
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def pick(row: dict[str, str], *aliases: str) -> str | None:
    for alias in aliases:
        value = row.get(normalize_header(alias))
        if value:
            return value
    return None


def read_tsv(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, delimiter="\t")
        for row in reader:
            normalized = {normalize_header(k or ""): clean(v) for k, v in row.items() if k}
            if any(normalized.values()):
                rows.append(normalized)
    return rows


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", value.strip())
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d %B %Y", "%d %b %Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_time(value: str | None) -> time | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", value.strip().upper())
    for fmt in ("%I:%M %p", "%I %p", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(normalized, fmt).time()
        except ValueError:
            continue
    return None


def combine_date_time(date_value: str | None, time_value: str | None) -> datetime | None:
    parsed_date = parse_date(date_value)
    if not parsed_date:
        return None

    parsed_time = parse_time(time_value) or time.min
    return datetime.combine(parsed_date.date(), parsed_time, tzinfo=timezone.utc)


async def import_company_master(master_sheet: str) -> dict[str, int]:
    await connect_to_mongo()
    await create_indexes()
    db = get_database()

    inserted = updated = skipped = 0
    for row in read_tsv(Path(master_sheet)):
        company_name = pick(row, "Company Name")
        role = pick(row, "Role") or "unknown"
        if not company_name:
            skipped += 1
            continue

        company_key = key(company_name)
        role_key = key(role)
        opportunity_received_on = pick(row, "Opportunity Received On")
        received_time = pick(row, "Received Time")
        opportunity_received_at = combine_date_time(opportunity_received_on, received_time)
        opportunity_key = key(
            opportunity_received_at.isoformat() if opportunity_received_at else f"{opportunity_received_on or 'no-date'}-{received_time or 'no-time'}"
        )
        now = datetime.now(timezone.utc)
        update = {
            "$set": {
                "company_name": company_name,
                "company_key": company_key,
                "role": role,
                "role_key": role_key,
                "opportunity_received_on": opportunity_received_on,
                "received_time": received_time,
                "opportunity_received_at": opportunity_received_at,
                "opportunity_key": opportunity_key,
                "crm_poc": pick(row, "CRM POC"),
                "student_side_status": pick(row, "Student Side Status"),
                "hubspot_link": pick(row, "Hubspot Link"),
                "student_response_sheet": pick(row, "Student Response Sheet"),
                "company_sheet": pick(row, "Company Sheet"),
                "positions": pick(row, "#Positions"),
                "profiles_requested": pick(row, "# Profile Requested"),
                "profiles_shared": pick(row, "# No .of Profiles shared"),
                "mapping_pool": pick(row, "#Mapping Pool"),
                "eligible_as_per_pref": pick(row, "# Eligible as per Pref"),
                "filled_form_count": pick(row, "# Filled Form"),
                "interested_count": pick(row, "# Interested"),
                "shortlists_count": pick(row, "# shortlists"),
                "company_status": pick(row, "Company Status"),
                "process_datetime": pick(row, "Date  & Time of Process"),
                "process_details": pick(row, "Company Process Details"),
                "screening_round": pick(row, "Screening Round/Telephonic Round"),
                "assignment_round": pick(row, "Assignement Round"),
                "tr_1": pick(row, "TR 1"),
                "next_process": pick(row, "Next Process"),
                "must_have_skills": pick(row, "Skills required (Must)"),
                "good_to_have_skills": pick(row, "Skills required (Good to Have)"),
                "stipend": pick(row, "Stipend"),
                "location": pick(row, "Location"),
                "duration": pick(row, "Duration"),
                "day_timings": pick(row, "Day & timings"),
                "company_feedback": pick(row, "Success Team_Company Feedback"),
                "scheduled_date": pick(row, "Scheduled Date"),
                "interview_process": pick(row, "Interview Process (e.g. TR, MR, Assessment)"),
                "action_items": pick(row, "Action Items"),
                "hiring_intelligence": pick(row, "Hiring Intelligence"),
                "rsa_notes": pick(row, "RSA"),
                "raw_company_row": row,
                "updated_at": now,
            },
            "$setOnInsert": {
                "source": "company_master_import",
                "created_at": now,
            },
        }
        result = await db[COMPANIES].update_one(
            {"company_key": company_key, "role_key": role_key, "opportunity_key": opportunity_key},
            update,
            upsert=True,
        )
        if result.upserted_id:
            inserted += 1
        else:
            updated += 1

    await close_mongo_connection()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import company master tracker sheet into MongoDB.")
    parser.add_argument("--master-sheet", required=True, help="Path to pasted TSV company tracker text file.")
    return parser.parse_args()


if __name__ == "__main__":
    print(asyncio.run(import_company_master(parse_args().master_sheet)))
