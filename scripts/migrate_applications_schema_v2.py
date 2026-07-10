import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import APPLICATIONS
from app.db.indexes import create_indexes
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database
from app.models.application import (
    build_application_details,
    default_placement,
    final_status_for,
    normalize_application_status,
)

BACKUP_COLLECTION = "applications_legacy_schema_v2_backup"

LEGACY_FIELDS = {
    "status": "",
    "is_interested": "",
    "skills": "",
    "has_relevant_project_experience": "",
    "github_link": "",
    "project_link": "",
    "resume_link": "",
    "willing_remote": "",
    "available_full_duration": "",
    "comfortable_stipend": "",
    "comfortable_schedule": "",
    "college_noc": "",
    "interest_reason": "",
    "not_interested_reason": "",
    "not_interested_other_reason": "",
    "response_snapshot": "",
    "raw_response": "",
    "role": "",
}


def compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in value.items() if v is not None}


def merged_application_details(application: dict[str, Any]) -> dict[str, Any]:
    existing = application.get("application_details") or {}
    legacy_other = compact_dict(
        {
            "not_interested_other_reason": application.get("not_interested_other_reason"),
            "response_snapshot": application.get("response_snapshot"),
            "raw_response": application.get("raw_response"),
        }
    )
    migrated = build_application_details(
        interested=existing.get("interested", application.get("is_interested")),
        skills=application.get("skills") or existing.get("self_assessment") or {},
        has_relevant_project_experience=existing.get(
            "has_relevant_project_experience",
            application.get("has_relevant_project_experience"),
        ),
        github_link=existing.get("github_link", application.get("github_link")),
        project_link=existing.get("project_link", application.get("project_link")),
        submitted_resume_url=existing.get("submitted_resume_url", application.get("resume_link")),
        willing_remote=existing.get("willing_remote", application.get("willing_remote")),
        available_full_duration=existing.get(
            "available_full_duration",
            application.get("available_full_duration"),
        ),
        comfortable_stipend=existing.get("comfortable_stipend", application.get("comfortable_stipend")),
        comfortable_schedule=existing.get("comfortable_schedule", application.get("comfortable_schedule")),
        college_noc=existing.get("college_noc", application.get("college_noc")),
        interest_reason=existing.get("interest_reason", application.get("interest_reason")),
        non_interest_reason=existing.get("non_interest_reason", application.get("not_interested_reason")),
        other_response={**(existing.get("other_response") or {}), **legacy_other},
    )
    return {**migrated, **{k: v for k, v in existing.items() if k not in migrated}}


def migrated_fields(application: dict[str, Any]) -> dict[str, Any]:
    details = merged_application_details(application)
    current_status = normalize_application_status(
        application.get("current_status") or application.get("status"),
        interested=details.get("interested"),
    )
    placement = application.get("placement") or default_placement()
    if current_status in {"SELECTED", "JOINED"}:
        placement["selected"] = True

    return {
        "source": application.get("source") or "response_sheet",
        "current_status": current_status,
        "final_status": application.get("final_status") or final_status_for(
            current_status,
            interested=details.get("interested"),
        ),
        "application_details": details,
        "placement": placement,
        "notes": application.get("notes"),
        "updated_at": datetime.now(timezone.utc),
    }


async def copy_backup_if_missing(db) -> int:
    if BACKUP_COLLECTION in await db.list_collection_names():
        return await db[BACKUP_COLLECTION].count_documents({})
    docs = await db[APPLICATIONS].find({}).to_list(length=None)
    if docs:
        await db[BACKUP_COLLECTION].insert_many(docs)
    return len(docs)


async def migrate(apply: bool, drop_legacy_fields: bool) -> dict[str, Any]:
    await connect_to_mongo()
    db = get_database()
    applications = await db[APPLICATIONS].find({}).to_list(length=None)

    needs_schema_update = 0
    already_migrated = 0
    would_unset_or_unset = 0
    for application in applications:
        has_new_shape = (
            "current_status" in application
            and isinstance(application.get("application_details"), dict)
            and isinstance(application.get("placement"), dict)
        )
        if has_new_shape:
            already_migrated += 1
        else:
            needs_schema_update += 1
        if drop_legacy_fields and any(field in application for field in LEGACY_FIELDS):
            would_unset_or_unset += 1

    backup_count = None
    if apply:
        backup_count = await copy_backup_if_missing(db)
        for application in applications:
            update_doc: dict[str, Any] = {"$set": migrated_fields(application)}
            if drop_legacy_fields:
                update_doc["$unset"] = LEGACY_FIELDS
            await db[APPLICATIONS].update_one({"_id": application["_id"]}, update_doc)
        await create_indexes()

    final_missing_new_shape = await db[APPLICATIONS].count_documents(
        {
            "$or": [
                {"current_status": {"$exists": False}},
                {"application_details": {"$exists": False}},
                {"placement": {"$exists": False}},
            ]
        }
    )

    await close_mongo_connection()
    return {
        "mode": "apply" if apply else "dry_run",
        "applications_checked": len(applications),
        "needs_schema_update": needs_schema_update,
        "already_migrated": already_migrated,
        "would_unset_or_unset_legacy_fields": would_unset_or_unset,
        "backup_collection": BACKUP_COLLECTION,
        "backup_count": backup_count,
        "final_missing_new_shape": final_missing_new_shape,
        "note": (
            "Run first without --drop-legacy-fields. After validation passes, rerun with "
            "--apply --drop-legacy-fields to remove redundant top-level fields."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate applications to the nested Stage 2 schema.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this, only prints a dry run.")
    parser.add_argument(
        "--drop-legacy-fields",
        action="store_true",
        help="Unset old top-level application fields after the new schema has been written.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    import json

    args = parse_args()
    print(json.dumps(asyncio.run(migrate(args.apply, args.drop_legacy_fields)), default=str, indent=2))
