import argparse
import asyncio
import csv
import io
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import APPLICATIONS, COMPANIES, HIRING_OPPORTUNITIES, STATUS_HISTORY, STUDENTS
from app.db.indexes import create_indexes
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database
from app.models.student import build_student_document
from app.services.student_service import normalize_email, normalize_phone
from app.utils.password import hash_password


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def read_tsv(path: Path) -> list[dict[str, str]]:
    raw = path.read_text(encoding="utf-8-sig")
    lines = raw.splitlines(keepends=True)
    while lines and not lines[0].strip():  # drop leading blank lines before the header row
        lines.pop(0)
    reader = csv.DictReader(io.StringIO("".join(lines)), delimiter="\t")
    rows: list[dict[str, str]] = []
    for row in reader:
        normalized = {normalize_header(k or ""): clean(v) for k, v in row.items() if k}
        if any(normalized.values()):
            rows.append(normalized)
    return rows


def pick(row: dict[str, str], *aliases: str) -> str | None:
    for alias in aliases:
        value = row.get(normalize_header(alias))
        if value:
            return value
    return None


def pick_prefix(row: dict[str, str], *prefixes: str) -> str | None:
    """Match by normalized-header prefix, for columns whose text varies per opportunity
    (e.g. 'Are you willing to work in <Location>?')."""
    for prefix in prefixes:
        normalized_prefix = normalize_header(prefix)
        for header, value in row.items():
            if value and header.startswith(normalized_prefix):
                return value
    return None


def parse_rating(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return {"score": int(match.group(0)) if match else None, "label": value}


def extract_skills(row: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Skill-rating columns always sit in one block between the 'interested' question and
    'Do you have relevant project experience?'. Stop at that boundary so a stray
    'Skill Assessment Ratings [S]' tacked on elsewhere in the sheet is ignored."""
    skills: dict[str, dict[str, Any]] = {}
    for header, value in row.items():
        if header == "do_you_have_relevant_project_experience":
            break
        match = re.match(r"skill_assessment_ratings_(.+)", header)
        if match and value:
            rating = parse_rating(value)
            if rating:
                skills[match.group(1).strip("_")] = rating
    return skills


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%d-%b-%Y", "%b-%d-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", value.strip())
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%b-%d-%Y", "%B-%d-%Y", "%d %B %Y", "%d %b %Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def resolve_opportunity(db, company_name: str, received_on: str, role: str | None) -> dict:
    """Find the ONE master opportunity these responses belong to. Never creates anything."""
    company = await db[COMPANIES].find_one({"company_key": key(company_name)})
    if not company:
        raise RuntimeError(
            f"Company '{company_name}' not found. Import the master sheet first (import_company_master.py)."
        )

    opportunities = await db[HIRING_OPPORTUNITIES].find({"company_id": company["_id"]}).to_list(length=None)
    target_date = parse_date(received_on)

    def matches_date(opp: dict) -> bool:
        if opp.get("opportunity_received_on") and key(opp["opportunity_received_on"]) == key(received_on):
            return True
        opp_date = parse_date(opp.get("opportunity_received_on"))
        return bool(target_date and opp_date and opp_date.date() == target_date.date())

    # No date given -> only unambiguous when the company has a single opening.
    candidates = [opp for opp in opportunities if matches_date(opp)] if received_on else list(opportunities)
    if role:
        candidates = [opp for opp in candidates if opp.get("role_key") == key(role)]

    if not candidates:
        available = ", ".join(f"{o.get('role')} ({o.get('opportunity_received_on')})" for o in opportunities) or "none"
        raise RuntimeError(
            f"No master opportunity for '{company_name}' on '{received_on}'"
            f"{f' role={role}' if role else ''}. Available openings: {available}"
        )
    if len(candidates) > 1:
        listed = ", ".join(f"{o.get('role')} ({o.get('opportunity_received_on')})" for o in candidates)
        raise RuntimeError(
            f"Multiple openings match '{company_name}' on '{received_on}': {listed}. Pass --role to disambiguate."
        )
    return candidates[0]


async def upsert_student(db, row: dict[str, str]) -> ObjectId | None:
    now = datetime.now(timezone.utc)
    external_user_id = pick(row, "Student UID", "User ID", "Student ID")
    name = pick(row, "Student Name", "Candidate Name", "Name")
    email = normalize_email(pick(row, "Email", "Email ID"))
    phone = normalize_phone(pick(row, "Mobile Number", "Phone", "Mobile") or "")
    resume_link = pick(row, "Resume Link (Shareable Drive Link with 'Viewer' Access)", "Resume Link", "Resume")

    if not name or not phone:
        return None

    query_options = [{"phone": phone}]
    if external_user_id:
        query_options.insert(0, {"external_user_id": external_user_id})
    existing = await db[STUDENTS].find_one({"$or": query_options})

    update_fields = {
        "external_user_id": external_user_id,
        "name": name,
        "phone": phone,
        "resume_link": resume_link,
        "current_city": pick(row, "Current City"),
        "college_name": pick(row, "College Name"),
        "degree": pick(row, "Degree (e.g., B.Tech, M.Tech, BCA, etc.)", "Degree"),
        "department": pick(row, "Department (e.g., CSE, ECE, IT)", "Department"),
        "year_of_passing": pick(row, "Year of Passing"),
        "technical_developer_name": pick(row, "Mention your Techincal Developer Name.", "Technical Developer Name"),
        "updated_at": now,
    }
    if email:
        update_fields["email"] = email

    if existing:
        await db[STUDENTS].update_one({"_id": existing["_id"]}, {"$set": {k: v for k, v in update_fields.items() if v is not None}})
        return existing["_id"]

    document = build_student_document(
        external_user_id=external_user_id, name=name, email=email, phone=phone,
        stack=None, resume_link=resume_link, password_hash=hash_password(phone),
    )
    document.update({k: v for k, v in update_fields.items() if v is not None})
    result = await db[STUDENTS].insert_one(document)
    return result.inserted_id


def build_application(row: dict[str, str], *, opportunity: dict, company: dict, student_id: ObjectId) -> dict[str, Any]:
    interested = (pick(row, "Are you interested in applying for this internship opportunity ?") or "").lower() == "yes"
    return {
        "student_id": student_id,
        "company_id": company["_id"],
        "opportunity_id": opportunity["_id"],
        "role": opportunity.get("role"),  # role always from the master opportunity
        "status": "applied" if interested else "not_interested",
        "is_interested": interested,
        "applied_at": parse_timestamp(pick(row, "Timestamp")),
        "skills": extract_skills(row),
        "has_relevant_project_experience": pick(row, "Do you have relevant project experience?"),
        "github_link": pick(row, "GitHub Profile Link (Ensure it is public)", "GitHub Profile Link"),
        "project_link": pick_prefix(row, "Project Link"),
        "resume_link": pick(row, "Resume Link (Shareable Drive Link with 'Viewer' Access)", "Resume Link"),
        "willing_remote": pick_prefix(row, "Are you willing to work in"),
        "available_full_duration": pick_prefix(row, "Are you available for the full"),
        "comfortable_stipend": pick_prefix(row, "Are you comfortable with the stipend"),
        "comfortable_schedule": pick_prefix(row, "Are you comfortable with the specified work schedule"),
        "college_noc": pick(row, "Will your college allow you to proceed with this internship (NOC)?"),
        "interest_reason": pick_prefix(row, "Why are you interested"),
        "not_interested_reason": pick(row, "Reason (If NOT Applying) - Please select the primary reason for non-interest."),
        "not_interested_other_reason": pick(row, "If 'Other' reason was selected, please specify:"),
        "response_snapshot": {
            "student_uid": pick(row, "Student UID", "User ID", "Student ID"),
            "student_name": pick(row, "Student Name", "Candidate Name", "Name"),
            "email": normalize_email(pick(row, "Email", "Email ID")),
            "mobile": normalize_phone(pick(row, "Mobile Number", "Phone", "Mobile") or ""),
            "company_name": company.get("name"),
            "role": opportunity.get("role"),
        },
        "raw_response": row,
    }


async def import_response_sheet(args: argparse.Namespace) -> dict[str, Any]:
    await connect_to_mongo()
    if not args.dry_run:
        await create_indexes()
    db = get_database()

    opportunity = await resolve_opportunity(db, args.company_name, args.received_on, args.role)
    company = await db[COMPANIES].find_one({"_id": opportunity["company_id"]})
    rows = read_tsv(Path(args.response_sheet))

    inserted = updated = skipped = 0
    for row in rows:
        student_uid = pick(row, "Student UID", "User ID", "Student ID")
        if args.dry_run:
            name = pick(row, "Student Name", "Candidate Name", "Name")
            phone = normalize_phone(pick(row, "Mobile Number", "Phone", "Mobile") or "")
            if name and phone:
                inserted += 1
            else:
                skipped += 1
            continue

        student_id = await upsert_student(db, row)
        if not student_id:
            skipped += 1
            continue

        application = build_application(row, opportunity=opportunity, company=company, student_id=student_id)
        now = datetime.now(timezone.utc)
        existing = await db[APPLICATIONS].find_one({"opportunity_id": opportunity["_id"], "student_id": student_id})
        if existing:
            await db[APPLICATIONS].update_one({"_id": existing["_id"]}, {"$set": {**application, "updated_at": now}})
            updated += 1
        else:
            application["created_at"] = now
            application["updated_at"] = now
            result = await db[APPLICATIONS].insert_one(application)
            await db[STATUS_HISTORY].insert_one({
                "application_id": result.inserted_id,
                "student_id": student_id,
                "company_id": company["_id"],
                "opportunity_id": opportunity["_id"],
                "old_status": None,
                "new_status": application["status"],
                "reason": "Application imported from response sheet",
                "changed_by": None,
                "changed_by_role": "system",
                "source": "response_import",
                "created_at": now,
            })
            inserted += 1

    await close_mongo_connection()
    return {
        "mode": "dry_run" if args.dry_run else "apply",
        "company": company.get("name"),
        "opportunity_role": opportunity.get("role"),
        "opportunity_received_on": opportunity.get("opportunity_received_on"),
        "rows_read": len(rows),
        "applications_inserted": inserted,
        "applications_updated": updated,
        "skipped": skipped,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attach a company's student responses to an EXISTING master opportunity (never creates opportunities)."
    )
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--received-on", required=True, help="The master opening's 'Opportunity Received On' value, e.g. 7-Apr-2026.")
    parser.add_argument("--role", default=None, help="Only needed to disambiguate when a company has two openings on the same date.")
    parser.add_argument("--response-sheet", required=True, help="Path to pasted TSV response sheet text file.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report without writing.")
    return parser.parse_args()


if __name__ == "__main__":
    import json

    print(json.dumps(asyncio.run(import_response_sheet(parse_args())), default=str, indent=2))
