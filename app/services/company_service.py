import re
from typing import Any

from bson import ObjectId
from fastapi import HTTPException, status

from app.db.collections import COMPANIES, COMPANY_APPLICATIONS, COMPANY_SHORTLISTS
from app.db.mongodb import get_database
from app.utils.mongo import serialize_mongo
from app.utils.object_id import to_object_id


def company_text_filter(company_name: str | None = None, role: str | None = None) -> dict:
    filters: dict[str, Any] = {}
    if company_name:
        filters["company_name"] = {"$regex": re.escape(company_name.strip()), "$options": "i"}
    if role:
        filters["role"] = {"$regex": re.escape(role.strip()), "$options": "i"}
    return filters


async def list_companies(
    *,
    search: str | None = None,
    company_name: str | None = None,
    role: str | None = None,
    limit: int = 100,
) -> list[dict]:
    db = get_database()
    filters = company_text_filter(company_name=company_name, role=role)
    if search:
        filters["$or"] = [
            {"company_name": {"$regex": re.escape(search.strip()), "$options": "i"}},
            {"role": {"$regex": re.escape(search.strip()), "$options": "i"}},
            {"must_have_skills": {"$regex": re.escape(search.strip()), "$options": "i"}},
        ]

    companies = await db[COMPANIES].find(filters).sort("updated_at", -1).limit(limit).to_list(length=limit)
    return serialize_mongo(companies)


async def get_company(company_id: str) -> dict:
    db = get_database()
    try:
        object_id = to_object_id(company_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid company id")

    company = await db[COMPANIES].find_one({"_id": object_id})
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return serialize_mongo(company)


async def find_company_id(company_id: str | None, company_name: str | None, role: str | None) -> ObjectId | None:
    db = get_database()
    if company_id:
        try:
            return to_object_id(company_id)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid company id")

    filters = company_text_filter(company_name=company_name, role=role)
    if not filters:
        return None

    company = await db[COMPANIES].find_one(filters, {"_id": 1})
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return company["_id"]


async def list_applications(
    *,
    company_id: str | None = None,
    company_name: str | None = None,
    role: str | None = None,
    status_value: str | None = None,
    is_interested: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    db = get_database()
    filters: dict[str, Any] = {}
    resolved_company_id = await find_company_id(company_id, company_name, role)
    if resolved_company_id:
        filters["company_id"] = resolved_company_id
    if status_value:
        filters["status"] = status_value
    if is_interested is not None:
        filters["is_interested"] = is_interested

    applications = await db[COMPANY_APPLICATIONS].find(filters).sort("updated_at", -1).limit(limit).to_list(length=limit)
    return serialize_mongo(applications)


async def list_shortlists(
    *,
    company_id: str | None = None,
    company_name: str | None = None,
    role: str | None = None,
    matched_application: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    db = get_database()
    filters: dict[str, Any] = {}
    resolved_company_id = await find_company_id(company_id, company_name, role)
    if resolved_company_id:
        filters["company_id"] = resolved_company_id
    if matched_application is not None:
        filters["matched_application"] = matched_application

    shortlists = await db[COMPANY_SHORTLISTS].find(filters).sort("updated_at", -1).limit(limit).to_list(length=limit)
    return serialize_mongo(shortlists)


async def get_import_summary(company_name: str | None = None, role: str | None = None) -> dict:
    db = get_database()
    company_filter = company_text_filter(company_name=company_name, role=role)
    companies = await db[COMPANIES].find(company_filter, {"_id": 1}).to_list(length=None)
    company_ids = [company["_id"] for company in companies]

    app_filter = {"company_id": {"$in": company_ids}} if company_ids else {}
    shortlist_filter = {"company_id": {"$in": company_ids}} if company_ids else {}

    return {
        "companies": len(company_ids) if company_filter else await db[COMPANIES].count_documents({}),
        "applications": await db[COMPANY_APPLICATIONS].count_documents(app_filter),
        "interested_applications": await db[COMPANY_APPLICATIONS].count_documents({**app_filter, "is_interested": True}),
        "not_interested_applications": await db[COMPANY_APPLICATIONS].count_documents({**app_filter, "is_interested": False}),
        "shortlists": await db[COMPANY_SHORTLISTS].count_documents(shortlist_filter),
        "matched_shortlists": await db[COMPANY_SHORTLISTS].count_documents({**shortlist_filter, "matched_application": True}),
        "unmatched_shortlists": await db[COMPANY_SHORTLISTS].count_documents({**shortlist_filter, "matched_application": False}),
        "shortlisted_applications": await db[COMPANY_APPLICATIONS].count_documents({**app_filter, "status": "shortlisted"}),
    }


async def list_repeated_companies(limit: int = 100) -> list[dict]:
    db = get_database()
    pipeline = [
        {
            "$group": {
                "_id": "$company_key",
                "company_name": {"$first": "$company_name"},
                "count": {"$sum": 1},
                "roles": {"$addToSet": "$role"},
                "appearances": {
                    "$push": {
                        "company_id": "$_id",
                        "role": "$role",
                        "opportunity_received_on": "$opportunity_received_on",
                        "received_time": "$received_time",
                        "opportunity_received_at": "$opportunity_received_at",
                        "company_status": "$company_status",
                    }
                },
            }
        },
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1, "company_name": 1}},
        {"$limit": limit},
    ]
    repeated = await db[COMPANIES].aggregate(pipeline).to_list(length=limit)
    return serialize_mongo(repeated)
