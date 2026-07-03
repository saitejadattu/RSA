import csv
import io
import re
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.config.settings import get_settings
from app.db.collections import STUDENTS
from app.db.mongodb import get_database
from app.models.student import build_student_document
from app.schemas.student import StudentCreate
from app.utils.object_id import serialize_document
from app.utils.password import hash_password


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def normalize_email(email: str | None) -> str | None:
    return email.strip().lower() if email else None


def student_query(identifier: str) -> dict:
    cleaned = identifier.strip()
    if "@" in cleaned:
        return {"email": normalize_email(cleaned)}
    return {"phone": normalize_phone(cleaned)}


async def find_student_by_identifier(identifier: str) -> dict | None:
    db = get_database()
    return await db[STUDENTS].find_one(student_query(identifier))


async def list_students_for_debug(limit: int = 100, identifier: str | None = None) -> list[dict]:
    db = get_database()
    if identifier:
        student = await find_student_by_identifier(identifier)
        return [serialize_document(student)] if student else []

    cursor = db[STUDENTS].find({}).sort("created_at", -1).limit(limit)
    students = await cursor.to_list(length=limit)
    return [serialize_document(student) for student in students]


async def create_student(student: StudentCreate) -> dict:
    db = get_database()
    phone = normalize_phone(student.phone)
    if not phone:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Valid phone is required")

    document = build_student_document(
        name=student.name.strip(),
        external_user_id=student.external_user_id,
        email=normalize_email(str(student.email)) if student.email else None,
        phone=phone,
        stack=student.stack,
        resume_link=student.resume_link,
        password_hash=hash_password(phone),
    )
    try:
        result = await db[STUDENTS].insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Student already exists")
    document["_id"] = result.inserted_id
    return serialize_document(document)


def google_sheet_csv_url(sheet_url: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_url)
    if not match:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid Google Sheet URL")
    return f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=csv"


def pick_value(row: dict, aliases: tuple[str, ...]) -> str | None:
    normalized = {key.strip().lower().replace(" ", "_"): value for key, value in row.items()}
    for alias in aliases:
        value = normalized.get(alias)
        if value:
            return value.strip()
    return None


async def import_students_from_sheet(sheet_url: str | None = None) -> dict:
    settings = get_settings()
    source_url = sheet_url or settings.student_sheet_url
    if not source_url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Sheet URL is required")

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(google_sheet_csv_url(source_url))
        response.raise_for_status()

    reader = csv.DictReader(io.StringIO(response.text))
    inserted = updated = skipped = 0
    db = get_database()

    for row in reader:
        external_user_id = pick_value(row, ("user_id", "student_id", "candidate_id", "id"))
        name = pick_value(row, ("name", "student_name", "candidate_name", "full_name"))
        phone = normalize_phone(pick_value(row, ("phone", "mobile", "mobile_number", "phone_number", "student_phone")) or "")
        email = normalize_email(pick_value(row, ("email", "mail", "email_id", "student_email")))
        stack = pick_value(row, ("stack", "course", "technology", "domain"))
        resume_link = pick_value(row, ("resume_link", "resume", "cv", "drive_link"))

        if not name or not phone:
            skipped += 1
            continue

        now = datetime.now(timezone.utc)
        existing = await db[STUDENTS].find_one({"phone": phone})
        if existing:
            await db[STUDENTS].update_one(
                {"_id": existing["_id"]},
                {
                    "$set": {
                        "name": name,
                        "external_user_id": external_user_id,
                        "email": email,
                        "stack": stack,
                        "resume_link": resume_link,
                        "updated_at": now,
                    }
                },
            )
            updated += 1
            continue

        document = build_student_document(
            name=name,
            external_user_id=external_user_id,
            email=email,
            phone=phone,
            stack=stack,
            resume_link=resume_link,
            password_hash=hash_password(phone),
        )
        try:
            await db[STUDENTS].insert_one(document)
            inserted += 1
        except DuplicateKeyError:
            skipped += 1

    return {"inserted": inserted, "updated": updated, "skipped": skipped}
