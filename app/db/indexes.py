from pymongo import ASCENDING
from pymongo.errors import OperationFailure

from app.db.collections import COMPANIES, COMPANY_APPLICATIONS, COMPANY_SHORTLISTS, STUDENTS
from app.db.mongodb import get_database


async def create_indexes() -> None:
    db = get_database()
    try:
        await db[STUDENTS].drop_index("email_1")
    except OperationFailure:
        pass

    await db[STUDENTS].create_index(
        [("email", ASCENDING)],
        unique=True,
        partialFilterExpression={"email": {"$type": "string"}},
    )
    await db[STUDENTS].create_index([("phone", ASCENDING)], unique=True)
    await db[STUDENTS].create_index(
        [("external_user_id", ASCENDING)],
        unique=True,
        partialFilterExpression={"external_user_id": {"$type": "string"}},
    )
    await db[STUDENTS].create_index([("stack", ASCENDING)])
    await db[STUDENTS].create_index([("created_at", ASCENDING)])

    try:
        await db[COMPANIES].drop_index("company_key_1_role_key_1")
    except OperationFailure:
        pass

    await db[COMPANIES].create_index(
        [("company_key", ASCENDING), ("role_key", ASCENDING), ("opportunity_key", ASCENDING)],
        unique=True,
    )
    await db[COMPANIES].create_index([("company_name", ASCENDING)])
    await db[COMPANIES].create_index([("company_key", ASCENDING)])
    await db[COMPANIES].create_index([("role", ASCENDING)])
    await db[COMPANIES].create_index([("opportunity_received_at", ASCENDING)])
    await db[COMPANIES].create_index([("created_at", ASCENDING)])

    await db[COMPANY_APPLICATIONS].create_index(
        [("company_id", ASCENDING), ("student_uid", ASCENDING)],
        unique=True,
        partialFilterExpression={"student_uid": {"$type": "string"}},
    )
    await db[COMPANY_APPLICATIONS].create_index([("company_id", ASCENDING)])
    await db[COMPANY_APPLICATIONS].create_index([("student_id", ASCENDING)])
    await db[COMPANY_APPLICATIONS].create_index([("student_uid", ASCENDING)])
    await db[COMPANY_APPLICATIONS].create_index([("is_interested", ASCENDING)])
    await db[COMPANY_APPLICATIONS].create_index([("status", ASCENDING)])

    await db[COMPANY_SHORTLISTS].create_index([("company_id", ASCENDING)])
    await db[COMPANY_SHORTLISTS].create_index([("email", ASCENDING)])
    await db[COMPANY_SHORTLISTS].create_index([("status", ASCENDING)])
    await db[COMPANY_SHORTLISTS].create_index(
        [("company_id", ASCENDING), ("email", ASCENDING)],
        unique=True,
        partialFilterExpression={"email": {"$type": "string"}},
    )
