import argparse
import asyncio
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import COMPANIES, COMPANY_APPLICATIONS, STUDENTS
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
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, delimiter="\t")
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


def parse_rating(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return {
        "score": int(match.group(0)) if match else None,
        "label": value,
    }


def extract_skills(row: dict[str, str]) -> dict[str, dict[str, Any]]:
    skills: dict[str, dict[str, Any]] = {}
    for header, value in row.items():
        match = re.match(r"skill_assessment_ratings_(.+)", header)
        if match and value:
            skill_name = match.group(1).strip("_")
            rating = parse_rating(value)
            if rating:
                skills[skill_name] = rating
    return skills


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%d-%b-%Y", "%b-%d-%Y"):
        try:
            parsed = datetime.strptime(value.strip(), fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def upsert_company(args: argparse.Namespace) -> ObjectId:
    db = get_database()
    now = datetime.now(timezone.utc)
    company_key = key(args.company_name)
    role_key = key(args.role)

    update = {
        "$set": {
            "company_name": args.company_name.strip(),
            "company_key": company_key,
            "role": args.role.strip(),
            "role_key": role_key,
            "opportunity_key": "response-sheet",
            "tech_stack": args.tech_stack,
            "must_have_skills": args.must_have_skills,
            "source": "response_sheet_import",
            "updated_at": now,
        },
        "$setOnInsert": {
            "created_at": now,
        },
    }
    result = await db[COMPANIES].update_one(
        {"company_key": company_key, "role_key": role_key, "opportunity_key": "response-sheet"},
        update,
        upsert=True,
    )
    if result.upserted_id:
        return result.upserted_id

    company = await db[COMPANIES].find_one({"company_key": company_key, "role_key": role_key, "opportunity_key": "response-sheet"}, {"_id": 1})
    return company["_id"]


async def upsert_student(row: dict[str, str]) -> ObjectId | None:
    db = get_database()
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
    query = {"$or": query_options}
    existing = await db[STUDENTS].find_one(query)
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
        await db[STUDENTS].update_one({"_id": existing["_id"]}, {"$set": update_fields})
        return existing["_id"]

    document = build_student_document(
        external_user_id=external_user_id,
        name=name,
        email=email,
        phone=phone,
        stack=None,
        resume_link=resume_link,
        password_hash=hash_password(phone),
    )
    document.update({k: v for k, v in update_fields.items() if v is not None})
    result = await db[STUDENTS].insert_one(document)
    return result.inserted_id


def build_application_document(
    *,
    row: dict[str, str],
    company_id: ObjectId,
    student_id: ObjectId | None,
    company_name: str,
    role: str,
) -> dict[str, Any]:
    interested = (pick(row, "Are you interested in applying for this internship opportunity ?") or "").lower() == "yes"
    applied_at = parse_timestamp(pick(row, "Timestamp"))
    return {
        "company_id": company_id,
        "company_name": company_name,
        "role": role,
        "student_id": student_id,
        "student_uid": pick(row, "Student UID", "User ID", "Student ID"),
        "student_name": pick(row, "Student Name", "Candidate Name", "Name"),
        "email": normalize_email(pick(row, "Email", "Email ID")),
        "mobile": normalize_phone(pick(row, "Mobile Number", "Phone", "Mobile") or ""),
        "is_interested": interested,
        "status": "applied" if interested else "not_interested",
        "not_interested_reason": pick(row, "Reason (If NOT Applying) - Please select the primary reason for non-interest."),
        "not_interested_other_reason": pick(row, "If 'Other' reason was selected, please specify:"),
        "skills": extract_skills(row),
        "has_relevant_project_experience": pick(row, "Do you have relevant project experience?"),
        "github_link": pick(row, "GitHub Profile Link (Ensure it is public)", "GitHub Profile Link"),
        "project_link": pick(row, "Project Link (Specific Academy / relevant project link)", "Project Link (Specific NIAT / relevant project link)", "Project Link"),
        "resume_link": pick(row, "Resume Link (Shareable Drive Link with 'Viewer' Access)", "Resume Link"),
        "willing_remote": pick(row, "Are you willing to work in Remote?"),
        "available_full_duration": pick(row, "Are you available for the full 2 Months duration?"),
        "college_noc": pick(row, "Will your college allow you to proceed with this internship (NOC)?"),
        "interest_reason": next((value for header, value in row.items() if header.startswith("why_are_you_interested")), None),
        "applied_at": applied_at,
        "raw_response": row,
    }


async def import_response_sheet(args: argparse.Namespace) -> dict[str, int]:
    await connect_to_mongo()
    await create_indexes()
    db = get_database()

    company_id = await upsert_company(args)
    rows = read_tsv(Path(args.response_sheet))

    inserted_students = updated_applications = inserted_applications = skipped = 0
    for row in rows:
        student_id = await upsert_student(row)
        student_uid = pick(row, "Student UID", "User ID", "Student ID")
        mobile = normalize_phone(pick(row, "Mobile Number", "Phone", "Mobile") or "")
        if not student_id or not student_uid:
            skipped += 1
            continue

        application = build_application_document(
            row=row,
            company_id=company_id,
            student_id=student_id,
            company_name=args.company_name.strip(),
            role=args.role.strip(),
        )
        existing = await db[COMPANY_APPLICATIONS].find_one({"company_id": company_id, "student_uid": student_uid})
        now = datetime.now(timezone.utc)
        if existing:
            await db[COMPANY_APPLICATIONS].update_one(
                {"_id": existing["_id"]},
                {
                    "$set": {
                        **application,
                        "updated_at": now,
                    },
                    "$push": {
                        "response_history": {
                            "imported_at": now,
                            "raw_response": row,
                        }
                    },
                },
            )
            updated_applications += 1
        else:
            application["created_at"] = now
            application["updated_at"] = now
            await db[COMPANY_APPLICATIONS].insert_one(application)
            inserted_applications += 1

        if mobile:
            inserted_students += 1

    await close_mongo_connection()
    return {
        "rows_read": len(rows),
        "students_seen": inserted_students,
        "applications_inserted": inserted_applications,
        "applications_updated": updated_applications,
        "skipped": skipped,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import a company response sheet into MongoDB.")
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--tech-stack", default=None)
    parser.add_argument("--must-have-skills", default=None)
    parser.add_argument("--response-sheet", required=True, help="Path to pasted TSV response sheet text file.")
    return parser.parse_args()


if __name__ == "__main__":
    print(asyncio.run(import_response_sheet(parse_args())))
