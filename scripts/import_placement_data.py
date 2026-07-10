import argparse
import asyncio
import csv
import difflib
import io
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import APPLICATIONS, COMPANIES, HIRING_OPPORTUNITIES, STATUS_HISTORY, STUDENTS
from app.db.indexes import create_indexes
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database
from app.models.application import build_application_details, default_placement, final_status_for, status_for_api
from app.services.student_service import normalize_email, normalize_phone


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "NA":
        return None
    return text


def key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def company_match_key(value: str | None) -> str:
    text = key(value)
    parts = [part for part in text.split("-") if part]
    suffixes = {"pvt", "ltd", "private", "limited", "llp", "india"}
    while parts and parts[-1] in suffixes:
        parts.pop()
    return "-".join(parts)


def compact_company_key(value: str | None) -> str:
    return company_match_key(value).replace("-", "")


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def read_rows(path: Path) -> list[dict[str, str | None]]:
    raw = path.read_text(encoding="utf-8-sig")
    lines = raw.splitlines(keepends=True)
    while lines and not lines[0].strip():
        lines.pop(0)
    sample = "".join(lines[:3])
    delimiter = "\t" if sample.count("\t") >= sample.count(",") else ","
    reader = csv.DictReader(io.StringIO("".join(lines)), delimiter=delimiter)
    return [
        {normalize_header(k or ""): clean(v) for k, v in row.items() if k}
        for row in reader
        if any(clean(v) for v in row.values())
    ]


def pick(row: dict[str, str | None], *aliases: str) -> str | None:
    for alias in aliases:
        value = row.get(normalize_header(alias))
        if value:
            return value
    return None


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", value.strip())
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_duration_months(value: str | None) -> int | None:
    if not value:
        return None
    numbers = re.findall(r"\d+", value)
    if len(numbers) == 1:
        return int(numbers[0])
    return None


def normalize_offer_status(value: str | None) -> str | None:
    text = key(value).replace("-", "_")
    if not text:
        return None
    if "accepted" in text:
        return "ACCEPTED"
    if "rejected" in text:
        return "REJECTED"
    if "released" in text and "yet" not in text:
        return "RELEASED"
    if "pending" in text or "yet_to_be_released" in text or "to_be_released" in text:
        return "PENDING"
    return None


def normalize_internship_status(value: str | None) -> str | None:
    text = key(value).replace("-", "_")
    if not text or text == "not_joined":
        return None
    if text == "yet_to_start":
        return "YET_TO_START"
    if text == "in_progress":
        return "IN_PROGRESS"
    if text == "completed":
        return "COMPLETED"
    if text.startswith("discontinued"):
        return "DISCONTINUED"
    if text.startswith("terminated"):
        return "TERMINATED"
    return None


def current_status_from_placement(offer_status: str | None, internship_status: str | None) -> str:
    if internship_status == "IN_PROGRESS":
        return "JOINED"
    if internship_status in {"DISCONTINUED", "TERMINATED"}:
        return "DROPPED"
    if offer_status == "ACCEPTED":
        return "OFFER_ACCEPTED"
    if offer_status == "REJECTED":
        return "OFFER_REJECTED"
    if offer_status == "RELEASED":
        return "OFFER_RELEASED"
    if offer_status == "PENDING":
        return "OFFER_PENDING"
    return "SELECTED"


async def find_student(db, row: dict[str, str | None]) -> dict | None:
    uid = pick(row, "UID", "Student UID", "User ID", "Student ID")
    email = normalize_email(pick(row, "Email Id", "Email", "Email ID"))
    phone = normalize_phone(pick(row, "Mobile Numbers", "Mobile Number", "Phone") or "")
    queries = []
    if uid:
        queries.append({"external_user_id": uid})
    if email:
        queries.append({"email": email})
    if phone:
        queries.append({"phone": phone})
    if not queries:
        return None
    return await db[STUDENTS].find_one({"$or": queries})


async def find_company(db, row: dict[str, str | None]) -> dict | None:
    company_name = pick(row, "Company Name")
    if not company_name:
        return None
    exact = await db[COMPANIES].find_one({"company_key": key(company_name)})
    if exact:
        return exact

    companies = await db[COMPANIES].find({}, {"name": 1, "company_key": 1, "aliases": 1}).to_list(length=None)
    target_keys = {company_match_key(company_name), compact_company_key(company_name)}
    candidates: list[dict] = []
    for company in companies:
        names = [company.get("name"), company.get("company_key"), *(company.get("aliases") or [])]
        company_keys = {company_match_key(name) for name in names if name}
        company_keys.update(compact_company_key(name) for name in names if name)
        if target_keys & company_keys:
            candidates.append(company)
    if len(candidates) == 1:
        return candidates[0]

    target = company_match_key(company_name)
    scored: list[tuple[float, dict]] = []
    for company in companies:
        names = [company.get("name"), company.get("company_key"), *(company.get("aliases") or [])]
        best = max(
            (difflib.SequenceMatcher(None, target, company_match_key(name)).ratio() for name in names if name),
            default=0,
        )
        if best >= 0.88:
            scored.append((best, company))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.03):
        return scored[0][1]
    return None


async def find_opportunity(db, company: dict, row: dict[str, str | None]) -> dict | None:
    role_key = key(pick(row, "Role"))
    query = {"company_id": company["_id"]}
    if role_key:
        query["role_key"] = role_key
    opportunities = await db[HIRING_OPPORTUNITIES].find(query).sort("opportunity_received_at", -1).to_list(length=2)
    if len(opportunities) == 1:
        return opportunities[0]
    if not role_key:
        return None
    fuzzy = await db[HIRING_OPPORTUNITIES].find({"company_id": company["_id"]}).sort("opportunity_received_at", -1).to_list(length=None)
    matches = [item for item in fuzzy if role_key in key(item.get("role")) or key(item.get("role")) in role_key]
    return matches[0] if len(matches) == 1 else None


async def find_application(db, student: dict, company: dict, opportunity: dict | None) -> dict | None:
    query = {"student_id": student["_id"], "company_id": company["_id"]}
    if opportunity:
        query["opportunity_id"] = opportunity["_id"]
    applications = await db[APPLICATIONS].find(query).sort("updated_at", -1).to_list(length=2)
    return applications[0] if len(applications) == 1 else None


def build_placement(row: dict[str, str | None]) -> tuple[dict, str]:
    offer_status = normalize_offer_status(pick(row, "Offer Letter Status"))
    internship_status = normalize_internship_status(pick(row, "Internship Status"))
    current_status = current_status_from_placement(offer_status, internship_status)
    placement = default_placement()
    placement["selected"] = True
    placement["offer_letter"] = {
        "status": offer_status,
        "url": pick(row, "Offer Letter Link"),
        "received_at": parse_date(pick(row, "Offer Letter Received Date")),
    }
    placement["internship"] = {
        "status": internship_status,
        "joining_date": parse_date(pick(row, "Date of Joining")),
        "stipend": pick(row, "Internship Stipend"),
        "duration_months": parse_duration_months(pick(row, "Internship Duration in Months")),
        "location": pick(row, "Job Location"),
    }
    return placement, current_status


async def import_placement_data(args: argparse.Namespace) -> dict[str, Any]:
    await connect_to_mongo()
    if not args.dry_run:
        await create_indexes()
    db = get_database()
    rows = read_rows(Path(args.placement_sheet))
    now = datetime.now(timezone.utc)

    updated = created = skipped = unmatched_student = unmatched_company = unmatched_application = ambiguous = 0
    for row in rows:
        student = await find_student(db, row)
        if not student:
            unmatched_student += 1
            continue
        company = await find_company(db, row)
        if not company:
            unmatched_company += 1
            continue
        opportunity = await find_opportunity(db, company, row)
        application = await find_application(db, student, company, opportunity)
        if not application and not args.create_missing:
            unmatched_application += 1
            continue
        if not application and not opportunity:
            ambiguous += 1
            continue

        placement, current_status = build_placement(row)
        details_update = {
            "application_details.other_response.placement_import": row,
            "placement": placement,
            "current_status": current_status,
            "final_status": final_status_for(current_status, interested=True),
            "updated_at": now,
        }

        if args.dry_run:
            updated += 1 if application else 0
            created += 1 if not application else 0
            continue

        if application:
            old_status = status_for_api(application)
            await db[APPLICATIONS].update_one({"_id": application["_id"]}, {"$set": details_update})
            application_id = application["_id"]
            updated += 1
        else:
            old_status = None
            doc = {
                "student_id": student["_id"],
                "company_id": company["_id"],
                "opportunity_id": opportunity["_id"],
                "applied_at": None,
                "source": "placement_sheet",
                "current_status": current_status,
                "final_status": final_status_for(current_status, interested=True),
                "application_details": build_application_details(
                    interested=True,
                    other_response={"placement_import": row},
                ),
                "placement": placement,
                "notes": None,
                "created_at": now,
                "updated_at": now,
            }
            result = await db[APPLICATIONS].insert_one(doc)
            application_id = result.inserted_id
            created += 1

        await db[STATUS_HISTORY].insert_one(
            {
                "application_id": application_id,
                "student_id": student["_id"],
                "company_id": company["_id"],
                "opportunity_id": opportunity["_id"] if opportunity else application.get("opportunity_id"),
                "old_status": old_status,
                "new_status": current_status,
                "reason": "Placement data imported from sheet",
                "notes": pick(row, "Internship Status"),
                "changed_by": None,
                "changed_by_role": "system",
                "source": "placement_import",
                "created_at": now,
            }
        )

    await close_mongo_connection()
    return {
        "mode": "dry_run" if args.dry_run else "apply",
        "rows_read": len(rows),
        "applications_updated": updated,
        "applications_created": created,
        "skipped": skipped,
        "unmatched_students": unmatched_student,
        "unmatched_companies": unmatched_company,
        "unmatched_applications": unmatched_application,
        "ambiguous_rows": ambiguous,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import placement lifecycle data into applications.placement.")
    parser.add_argument("--placement-sheet", required=True, help="Path to exported/pasted CSV or TSV placement sheet.")
    parser.add_argument("--create-missing", action="store_true", help="Create application rows only when no application exists.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and match rows without writing.")
    return parser.parse_args()


if __name__ == "__main__":
    import json

    print(json.dumps(asyncio.run(import_placement_data(parse_args())), default=str, indent=2))
