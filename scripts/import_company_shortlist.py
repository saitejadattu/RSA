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

from app.db.collections import APPLICATIONS, COMPANIES, HIRING_OPPORTUNITIES, STATUS_HISTORY, STUDENTS
from app.db.indexes import create_indexes
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database
from app.models.student import build_student_document
from app.services.student_service import normalize_email, normalize_phone
from app.utils.password import hash_password


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


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


def read_shortlist_rows(path: Path) -> list[list[str | None]]:
    """Read positionally (not by header): the notes column is unlabeled, and some rows
    are shifted (missing UID), so column-name matching is unreliable here."""
    rows: list[list[str | None]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        for cells in csv.reader(file, delimiter="\t"):
            cleaned = [clean(c) for c in cells]
            if not any(cleaned):
                continue
            first = (cleaned[0] or "").strip().lower()
            if first == "uid":  # header row (may repeat if sheets were concatenated)
                continue
            rows.append(cleaned)
    return rows


def find_in_cells(cells: list[str | None], predicate) -> str | None:
    for cell in cells:
        if cell and predicate(cell):
            return cell
    return None


def looks_like_phone(cell: str) -> bool:
    # phone = only phone characters (no letters) with 10-13 digits; excludes UID/UUID cells
    digits = re.sub(r"\D", "", cell)
    return 10 <= len(digits) <= 13 and bool(re.fullmatch(r"[\d\s+()\-]+", cell.strip()))


def normalize_willing(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if "not" in text and "interest" in text:
        return "not_interested"
    if "interest" in text:
        return "interested"
    return None


def extract_row(cells: list[str | None]) -> dict[str, Any]:
    """Best-effort field extraction robust to a missing/shifted UID."""
    uid = find_in_cells(cells, lambda c: bool(UUID_RE.match(c)))
    email = normalize_email(find_in_cells(cells, lambda c: "@" in c))
    phone = normalize_phone(find_in_cells(cells, looks_like_phone) or "")
    resume = find_in_cells(cells, lambda c: c.lower().startswith("http"))
    # willing is a constrained vocabulary — scan for it rather than trusting position
    willing_raw = find_in_cells(cells, lambda c: c.strip().lower() in ("interested", "not interested", "not intrested"))
    willing_index = cells.index(willing_raw) if willing_raw in cells else None
    notes = None
    if willing_index is not None:
        notes = " ".join(c for c in cells[willing_index + 1 :] if c) or None
    call_status = find_in_cells(cells, lambda c: "call" in c.lower())
    call_date = find_in_cells(cells, lambda c: bool(re.match(r"\d{1,2}/\d{1,2}/\d{2,4}", c.strip())))
    # name: first cell that is not the uid, not an email/url/number
    name = None
    for cell in cells:
        if cell and cell != uid and "@" not in cell and not cell.lower().startswith("http") and not re.fullmatch(r"[\d\s/:-]+", cell):
            name = cell
            break
    return {
        "uid": uid,
        "name": name,
        "email": email,
        "phone": phone,
        "resume": resume,
        "call_date": call_date,
        "call_status": call_status,
        "willing_to_join": normalize_willing(willing_raw),
        "willing_notes": notes,
        "raw_shortlist_row": cells,
    }


async def resolve_opportunity(db, company_name: str, received_on: str, role: str | None) -> dict:
    company = await db[COMPANIES].find_one({"company_key": key(company_name)})
    if not company:
        raise RuntimeError(f"Company '{company_name}' not found. Import the master sheet first.")
    opportunities = await db[HIRING_OPPORTUNITIES].find({"company_id": company["_id"]}).to_list(length=None)
    target_date = parse_date(received_on)

    def matches(opp: dict) -> bool:
        if opp.get("opportunity_received_on") and key(opp["opportunity_received_on"]) == key(received_on):
            return True
        opp_date = parse_date(opp.get("opportunity_received_on"))
        return bool(target_date and opp_date and opp_date.date() == target_date.date())

    candidates = [o for o in opportunities if matches(o)] if received_on else list(opportunities)
    if role:
        candidates = [o for o in candidates if o.get("role_key") == key(role)]
    if not candidates:
        available = ", ".join(f"{o.get('role')} ({o.get('opportunity_received_on')})" for o in opportunities) or "none"
        raise RuntimeError(f"No master opportunity for '{company_name}' on '{received_on}'. Available: {available}")
    if len(candidates) > 1:
        listed = ", ".join(f"{o.get('role')} ({o.get('opportunity_received_on')})" for o in candidates)
        raise RuntimeError(f"Multiple openings match on '{received_on}': {listed}. Pass --role.")
    return candidates[0]


async def find_student(db, data: dict) -> dict | None:
    for query in ([{"external_user_id": data["uid"]}] if data["uid"] else []) + (
        [{"email": data["email"]}] if data["email"] else []
    ) + ([{"phone": data["phone"]}] if data["phone"] else []):
        student = await db[STUDENTS].find_one(query)
        if student:
            return student
    return None


async def find_or_create_student(db, data: dict, now: datetime) -> ObjectId | None:
    student = await find_student(db, data)
    if student:
        return student["_id"]
    if not data["name"] or not data["phone"]:
        return None  # can't safely create without at least a name + phone (these resume sheets have no phone)
    document = build_student_document(
        external_user_id=data["uid"], name=data["name"], email=data["email"], phone=data["phone"],
        stack=None, resume_link=data["resume"], password_hash=hash_password(data["phone"]),
    )
    result = await db[STUDENTS].insert_one(document)
    return result.inserted_id


async def import_shortlist(args: argparse.Namespace) -> dict[str, Any]:
    await connect_to_mongo()
    if not args.dry_run:
        await create_indexes()
    db = get_database()

    opportunity = await resolve_opportunity(db, args.company_name, args.received_on, args.role)
    company = await db[COMPANIES].find_one({"_id": opportunity["company_id"]})
    rows = read_shortlist_rows(Path(args.shortlist_sheet))

    marked = created_apps = created_students = unmatched = 0
    willing_counts = {"interested": 0, "not_interested": 0, "no_response": 0}

    for cells in rows:
        data = extract_row(cells)
        willing_counts[data["willing_to_join"] or "no_response"] += 1

        if args.dry_run:
            student = await find_student(db, data)  # read-only match check
            if not student:
                unmatched += 1
            elif await db[APPLICATIONS].find_one({"opportunity_id": opportunity["_id"], "student_id": student["_id"]}):
                marked += 1
            else:
                created_apps += 1
            continue

        now = datetime.now(timezone.utc)
        student_id = await find_or_create_student(db, data, now)
        if not student_id:
            unmatched += 1
            continue

        shortlist_sub = {
            "is_shortlisted": True,
            "resume": data["resume"],
            "call_date": data["call_date"],
            "call_status": data["call_status"],
            "willing_to_join": data["willing_to_join"],  # interested / not_interested / None
            "willing_notes": data["willing_notes"],
            "source": "shortlist_sheet",
            "imported_at": now,
        }

        existing = await db[APPLICATIONS].find_one({"opportunity_id": opportunity["_id"], "student_id": student_id})
        if existing:
            old_status = existing.get("status")
            await db[APPLICATIONS].update_one(
                {"_id": existing["_id"]},
                {"$set": {"status": "shortlisted", "shortlisted_at": now, "shortlist": shortlist_sub, "updated_at": now}},
            )
            application_id = existing["_id"]
            marked += 1
        else:
            # Shortlisted but no application on file (response sheet was incomplete) — create it.
            doc = {
                "student_id": student_id,
                "company_id": company["_id"],
                "opportunity_id": opportunity["_id"],
                "role": opportunity.get("role"),
                "status": "shortlisted",
                "is_interested": True,
                "applied_at": None,
                "resume_link": data["resume"],
                "shortlist": shortlist_sub,
                "response_snapshot": {"student_name": data["name"], "email": data["email"], "company_name": company.get("name"), "role": opportunity.get("role")},
                "created_at": now,
                "updated_at": now,
            }
            result = await db[APPLICATIONS].insert_one(doc)
            application_id = result.inserted_id
            old_status = None
            created_apps += 1

        await db[STATUS_HISTORY].insert_one({
            "application_id": application_id,
            "student_id": student_id,
            "company_id": company["_id"],
            "opportunity_id": opportunity["_id"],
            "old_status": old_status,
            "new_status": "shortlisted",
            "reason": "Marked shortlisted from company shortlist sheet",
            "notes": data["willing_notes"],
            "changed_by": None,
            "changed_by_role": "system",
            "source": "shortlist_import",
            "created_at": now,
        })

    await close_mongo_connection()
    return {
        "mode": "dry_run" if args.dry_run else "apply",
        "company": company.get("name"),
        "opportunity_role": opportunity.get("role"),
        "rows_read": len(rows),
        "applications_marked_shortlisted": marked,
        "applications_created": created_apps,
        "students_created": created_students,
        "unmatched": unmatched,
        "willing_breakdown": willing_counts,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mark shortlisted students for a master opportunity (with willing-to-join).")
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--received-on", required=True, help="Master opening's 'Opportunity Received On', e.g. 7-Apr-2026.")
    parser.add_argument("--role", default=None, help="Disambiguate when a company has two openings on the same date.")
    parser.add_argument("--shortlist-sheet", required=True, help="Path to pasted TSV shortlist sheet text file.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report without writing.")
    return parser.parse_args()


if __name__ == "__main__":
    import json

    print(json.dumps(asyncio.run(import_shortlist(parse_args())), default=str, indent=2))
