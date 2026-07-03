import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from pymongo.errors import DuplicateKeyError

from app.db.collections import STUDENTS
from app.db.indexes import create_indexes
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database
from app.models.student import build_student_document
from app.services.student_service import normalize_phone
from app.utils.password import hash_password


TEST_STUDENTS = [
    {
        "external_user_id": "94d5563f-3aa7-414d-bff3-598cf0e38295",
        "name": "Yaswanth Vardhan",
        "phone": "7207100712",
    },
    {
        "external_user_id": "67fa3a20-f04c-4f90-9ef7-7c087ddace89",
        "name": "Krishna Srikar",
        "phone": "9666920866",
    },
    {
        "external_user_id": "f5d13f27-62bd-4713-be7f-959b3db663e4",
        "name": "Sai Chetan",
        "phone": "8074773591",
    },
    {
        "external_user_id": "819e2380-7e26-4fca-8b1a-8f2e93f65e7a",
        "name": "Gourishetti Ruthvik",
        "phone": "7842453961",
    },
    {
        "external_user_id": "a0799257-1bc9-465d-83c5-528d49e7bf73",
        "name": "Dwarakacherla Rishik Reddy",
        "phone": "9642350658",
    },
    {
        "external_user_id": "6c11749e-5871-495f-bafc-e8a4069c0752",
        "name": "A S Ao",
        "phone": "7013318998",
    },
    {
        "external_user_id": "2b46f9e3-514d-46c6-b5a8-31bfac4d3739",
        "name": "Subahan",
        "phone": "9652162995",
    },
    {
        "external_user_id": "d0f1a33e-277b-4b39-8a35-082ad10aacbd",
        "name": "Gnana Eswar",
        "phone": "9392716495",
    },
    {
        "external_user_id": "feee52db-aa69-48fb-92e4-9e313f24a474",
        "name": "Neeraj Gouda",
        "phone": "6304552554",
    },
    {
        "external_user_id": "9a14a470-4f1d-4148-8675-315cf7e9c857",
        "name": "Aakash Reddy",
        "phone": "9381816443",
    },
    {
        "external_user_id": "787b74f3-760f-472a-9282-004ea8c17d98",
        "name": "Yerram Reddy Hamsika",
        "phone": "8919818767",
    },
]


async def seed() -> dict:
    await connect_to_mongo()
    await create_indexes()

    db = get_database()
    inserted = updated = skipped = 0

    for student in TEST_STUDENTS:
        phone = normalize_phone(student["phone"])
        if not phone:
            skipped += 1
            continue

        existing = await db[STUDENTS].find_one({"phone": phone})
        if existing:
            await db[STUDENTS].update_one(
                {"_id": existing["_id"]},
                {
                    "$set": {
                        "external_user_id": student["external_user_id"],
                        "name": student["name"],
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            updated += 1
            continue

        document = build_student_document(
            external_user_id=student["external_user_id"],
            name=student["name"],
            email=None,
            phone=phone,
            stack=None,
            resume_link=None,
            password_hash=hash_password(phone),
        )
        try:
            await db[STUDENTS].insert_one(document)
            inserted += 1
        except DuplicateKeyError:
            skipped += 1

    await close_mongo_connection()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


if __name__ == "__main__":
    print(asyncio.run(seed()))
