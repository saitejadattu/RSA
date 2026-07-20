"""Import pasted response / shortlist sheets against a known opportunity.

Some company sheets could never be downloaded (HTTP 401, bad URLs), so this is
the manual path: an admin pastes the sheet straight into the opportunity page.

The parsing here is the same logic the CLI scripts use - see
scripts/import_company_response.py and scripts/import_company_shortlist.py,
which now delegate to this module so the two paths cannot drift apart.

Unlike the CLI, the opportunity is already known, so none of the fragile
company-name/received-on resolution is needed.
"""
import csv
import io
import re
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from fastapi import HTTPException, status

from app.db.collections import APPLICATIONS, COMPANIES, HIRING_OPPORTUNITIES, STATUS_HISTORY, STUDENTS
from app.db.mongodb import get_database
from app.models.application import (
    build_application_details,
    default_placement,
    final_status_for,
    normalize_application_status,
    status_for_api,
)
from app.models.student import build_student_document
from app.services.student_service import normalize_email, normalize_phone
from app.utils.mongo import serialize_mongo
from app.utils.object_id import to_object_id
from app.utils.password import hash_password

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# Statuses already past "applied". Re-importing a response sheet must not drag
# these back to APPLIED - the sheet only ever says someone applied, it knows
# nothing about interviews or offers that happened later.
AHEAD_OF_APPLIED = {
    "PROFILE_SHARED", "SHORTLISTED", "INTERVIEW_SCHEDULED", "INTERVIEW_IN_PROGRESS",
    "SELECTED", "OFFER_PENDING", "OFFER_RELEASED", "OFFER_ACCEPTED", "OFFER_REJECTED",
    "JOINED", "REJECTED", "DROPPED",
}


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _sniff_delimiter(sample: str) -> str:
    """Sheets paste as TSV; a CSV export is also accepted."""
    return "\t" if sample.count("\t") >= sample.count(",") else ","


# --------------------------------------------------------------------------
# response sheets - header based, because column wording differs per company
# --------------------------------------------------------------------------


def read_response_rows(raw_text: str) -> list[dict[str, str | None]]:
    lines = (raw_text or "").splitlines(keepends=True)
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return []
    delimiter = _sniff_delimiter("".join(lines[:3]))
    reader = csv.DictReader(io.StringIO("".join(lines)), delimiter=delimiter)
    rows: list[dict[str, str | None]] = []
    for row in reader:
        normalized = {normalize_header(k or ""): clean(v) for k, v in row.items() if k}
        if any(normalized.values()):
            rows.append(normalized)
    return rows


def pick(row: dict[str, str | None], *aliases: str) -> str | None:
    for alias in aliases:
        value = row.get(normalize_header(alias))
        if value:
            return value
    return None


def pick_prefix(row: dict[str, str | None], *prefixes: str) -> str | None:
    """Match by header prefix, for columns whose text varies per opportunity
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


def extract_skills(row: dict[str, str | None]) -> dict[str, dict[str, Any]]:
    """Skill columns sit between the 'interested' question and 'Do you have
    relevant project experience?'. Stop at that boundary so a stray rating
    column elsewhere in the sheet is ignored."""
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


def response_row_identity(row: dict[str, str | None]) -> dict[str, Any]:
    return {
        "uid": pick(row, "Student UID", "User ID", "Student ID"),
        "name": pick(row, "Student Name", "Candidate Name", "Name"),
        "email": normalize_email(pick(row, "Email", "Email ID")),
        "phone": normalize_phone(pick(row, "Mobile Number", "Phone", "Mobile") or ""),
        "resume": pick(row, "Resume Link (Shareable Drive Link with 'Viewer' Access)", "Resume Link", "Resume"),
    }


def build_application_fields(
    row: dict[str, str | None], *, opportunity: dict, company: dict, student_id, source: str = "response_sheet"
) -> dict:
    interested = (pick(row, "Are you interested in applying for this internship opportunity ?") or "").lower() == "yes"
    current_status = normalize_application_status(None, interested=interested)
    return {
        "student_id": student_id,
        "company_id": company["_id"],
        "opportunity_id": opportunity["_id"],
        "applied_at": parse_timestamp(pick(row, "Timestamp")),
        "source": source,
        "current_status": current_status,
        "final_status": final_status_for(current_status, interested=interested),
        "application_details": build_application_details(
            interested=interested,
            skills=extract_skills(row),
            has_relevant_project_experience=pick(row, "Do you have relevant project experience?"),
            github_link=pick(row, "GitHub Profile Link (Ensure it is public)", "GitHub Profile Link"),
            project_link=pick_prefix(row, "Project Link"),
            submitted_resume_url=pick(row, "Resume Link (Shareable Drive Link with 'Viewer' Access)", "Resume Link"),
            willing_remote=pick_prefix(row, "Are you willing to work in"),
            available_full_duration=pick_prefix(row, "Are you available for the full"),
            comfortable_stipend=pick_prefix(row, "Are you comfortable with the stipend"),
            comfortable_schedule=pick_prefix(row, "Are you comfortable with the specified work schedule"),
            college_noc=pick(row, "Will your college allow you to proceed with this internship (NOC)?"),
            interest_reason=pick_prefix(row, "Why are you interested"),
            non_interest_reason=pick(row, "Reason (If NOT Applying) - Please select the primary reason for non-interest."),
            other_response={
                "not_interested_other_reason": pick(row, "If 'Other' reason was selected, please specify:"),
                "raw_response": row,
            },
        ),
        "placement": default_placement(),
        "notes": None,
    }


def student_update_fields(row: dict[str, str | None], identity: dict) -> dict[str, Any]:
    return {
        "external_user_id": identity["uid"],
        "name": identity["name"],
        "phone": identity["phone"],
        "resume_link": identity["resume"],
        "current_city": pick(row, "Current City"),
        "college_name": pick(row, "College Name"),
        "degree": pick(row, "Degree (e.g., B.Tech, M.Tech, BCA, etc.)", "Degree"),
        "department": pick(row, "Department (e.g., CSE, ECE, IT)", "Department"),
        "year_of_passing": pick(row, "Year of Passing"),
        "technical_developer_name": pick(row, "Mention your Techincal Developer Name.", "Technical Developer Name"),
    }


# --------------------------------------------------------------------------
# shortlist sheets - positional, because the notes column is unlabeled and
# some rows are shifted (missing UID)
# --------------------------------------------------------------------------


def read_shortlist_rows(raw_text: str) -> list[list[str | None]]:
    lines = (raw_text or "").splitlines(keepends=True)
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return []
    delimiter = _sniff_delimiter("".join(lines[:3]))
    rows: list[list[str | None]] = []
    for cells in csv.reader(io.StringIO("".join(lines)), delimiter=delimiter):
        cleaned = [clean(c) for c in cells]
        if not any(cleaned):
            continue
        first = (cleaned[0] or "").strip().lower()
        if first in {"uid", "full name", "name"}:  # header, may repeat
            continue
        rows.append(cleaned)
    return rows


def find_in_cells(cells: list[str | None], predicate) -> str | None:
    for cell in cells:
        if cell and predicate(cell):
            return cell
    return None


def looks_like_phone(cell: str) -> bool:
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


def extract_shortlist_row(cells: list[str | None]) -> dict[str, Any]:
    """Best-effort extraction robust to a missing or shifted UID column."""
    uid = find_in_cells(cells, lambda c: bool(UUID_RE.match(c)))
    email = normalize_email(find_in_cells(cells, lambda c: "@" in c))
    phone = normalize_phone(find_in_cells(cells, looks_like_phone) or "")
    resume = find_in_cells(cells, lambda c: c.lower().startswith("http"))
    willing_raw = find_in_cells(
        cells, lambda c: c.strip().lower() in ("interested", "not interested", "not intrested")
    )
    willing_index = cells.index(willing_raw) if willing_raw in cells else None
    notes = None
    if willing_index is not None:
        notes = " ".join(c for c in cells[willing_index + 1:] if c) or None
    call_status = find_in_cells(cells, lambda c: "call" in c.lower())
    call_date = find_in_cells(cells, lambda c: bool(re.match(r"\d{1,2}/\d{1,2}/\d{2,4}", c.strip())))
    name = None
    for cell in cells:
        if (
            cell and cell != uid and "@" not in cell
            and not cell.lower().startswith("http")
            and not re.fullmatch(r"[\d\s/:-]+", cell)
        ):
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


# --------------------------------------------------------------------------
# shared lookups
# --------------------------------------------------------------------------


async def find_student(db, identity: dict) -> dict | None:
    queries = []
    if identity.get("uid"):
        queries.append({"external_user_id": identity["uid"]})
    if identity.get("email"):
        queries.append({"email": identity["email"]})
    if identity.get("phone"):
        queries.append({"phone": identity["phone"]})
    if not queries:
        return None
    return await db[STUDENTS].find_one({"$or": queries})


def name_key(value: str | None) -> str:
    """Compare names ignoring case, punctuation and spacing.
    'A.mohamed yusuff' and 'Mohamed Yusuff' both -> 'amohamedyusuff' / 'mohamedyusuff'."""
    return re.sub(r"[^a-z]+", "", (value or "").lower())


def _name_matches(sheet_name: str | None, student_name: str | None) -> bool:
    a, b = name_key(sheet_name), name_key(student_name)
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    if a == b:
        return True
    # One side often carries an initial or extra token the other omits
    # ("Sai Chaitanya" vs "Sai Chaitanya Reddy", "A.mohamed yusuff" vs "Mohamed yusuff").
    return a in b or b in a


async def build_applicant_name_index(db, opportunity_id) -> list[dict]:
    """Students who already have an application for THIS opening.

    Shortlist sheets frequently carry only a name - no UID, email or phone - so
    a name lookup is the only way to match them. It is deliberately scoped to
    this opening's applicants rather than all students: two different people
    called "Sai Kumar" across the whole database would otherwise collide, and
    marking the wrong student shortlisted is worse than reporting no match.
    """
    applications = await db[APPLICATIONS].find(
        {"opportunity_id": opportunity_id}, {"student_id": 1}
    ).to_list(length=None)
    student_ids = [application["student_id"] for application in applications]
    if not student_ids:
        return []
    return await db[STUDENTS].find(
        {"_id": {"$in": student_ids}}, {"name": 1, "email": 1, "phone": 1}
    ).to_list(length=None)


def match_by_name(sheet_name: str | None, applicants: list[dict]) -> tuple[dict | None, bool]:
    """Return (student, ambiguous). Ambiguous means several applicants share the
    name, so the admin must resolve it rather than us guessing."""
    hits = [student for student in applicants if _name_matches(sheet_name, student.get("name"))]
    if len(hits) == 1:
        return hits[0], False
    if len(hits) > 1:
        return None, True
    return None, False


async def load_opportunity(db, opportunity_id: str) -> tuple[dict, dict]:
    try:
        object_id = to_object_id(opportunity_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid opportunity id")
    opportunity = await db[HIRING_OPPORTUNITIES].find_one({"_id": object_id})
    if not opportunity:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    company = await db[COMPANIES].find_one({"_id": opportunity["company_id"]})
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return opportunity, company


def _keep_status(existing: dict | None, incoming: str) -> str:
    """A response sheet only ever proves someone applied. If the pipeline has
    already moved past that, keep where they are."""
    current = (existing or {}).get("current_status")
    return current if current in AHEAD_OF_APPLIED else incoming


# --------------------------------------------------------------------------
# responses
# --------------------------------------------------------------------------


async def import_responses(*, opportunity_id: str, raw_text: str, confirm: bool = False) -> dict:
    db = get_database()
    opportunity, company = await load_opportunity(db, opportunity_id)
    rows = read_response_rows(raw_text)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No rows found. Paste the sheet including its header row.",
        )

    now = datetime.now(timezone.utc)
    preview: list[dict[str, Any]] = []
    counts = {
        "rows": len(rows),
        "students_matched": 0,
        "students_to_create": 0,
        "applications_to_create": 0,
        "applications_to_update": 0,
        "status_preserved": 0,
        "skipped": 0,
    }

    for index, row in enumerate(rows, start=1):
        identity = response_row_identity(row)
        entry: dict[str, Any] = {
            "row": index,
            "name": identity["name"],
            "email": identity["email"],
            "phone": identity["phone"],
        }

        if not identity["name"] or not identity["phone"]:
            entry["action"] = "skip"
            entry["reason"] = "Row needs at least a name and a mobile number."
            counts["skipped"] += 1
            preview.append(entry)
            continue

        student = await find_student(db, identity)
        entry["student_exists"] = bool(student)
        if student:
            counts["students_matched"] += 1
        else:
            counts["students_to_create"] += 1

        existing_application = None
        if student:
            existing_application = await db[APPLICATIONS].find_one(
                {"opportunity_id": opportunity["_id"], "student_id": student["_id"]}
            )

        incoming_status = normalize_application_status(
            None,
            interested=(pick(row, "Are you interested in applying for this internship opportunity ?") or "").lower() == "yes",
        )
        kept_status = _keep_status(existing_application, incoming_status)
        entry["action"] = "update" if existing_application else "create"
        entry["status"] = kept_status
        if existing_application and kept_status != incoming_status:
            entry["status_preserved_from"] = existing_application.get("current_status")
            counts["status_preserved"] += 1
        counts["applications_to_update" if existing_application else "applications_to_create"] += 1

        if not confirm:
            preview.append(entry)
            continue

        # ---- write ----
        if student:
            update_fields = {k: v for k, v in student_update_fields(row, identity).items() if v is not None}
            if identity["email"]:
                update_fields["email"] = identity["email"]
            update_fields["updated_at"] = now
            await db[STUDENTS].update_one({"_id": student["_id"]}, {"$set": update_fields})
            student_id = student["_id"]
        else:
            document = build_student_document(
                external_user_id=identity["uid"], name=identity["name"], email=identity["email"],
                phone=identity["phone"], stack=None, resume_link=identity["resume"],
                password_hash=hash_password(identity["phone"]),
            )
            document.update({k: v for k, v in student_update_fields(row, identity).items() if v is not None})
            student_id = (await db[STUDENTS].insert_one(document)).inserted_id

        fields = build_application_fields(
            row, opportunity=opportunity, company=company, student_id=student_id, source="response_paste"
        )
        existing_application = await db[APPLICATIONS].find_one(
            {"opportunity_id": opportunity["_id"], "student_id": student_id}
        )
        fields["current_status"] = _keep_status(existing_application, fields["current_status"])
        fields["final_status"] = final_status_for(
            fields["current_status"], interested=fields["application_details"].get("interested")
        )

        if existing_application:
            await db[APPLICATIONS].update_one(
                {"_id": existing_application["_id"]}, {"$set": {**fields, "updated_at": now}}
            )
        else:
            fields["created_at"] = now
            fields["updated_at"] = now
            result = await db[APPLICATIONS].insert_one(fields)
            await db[STATUS_HISTORY].insert_one({
                "application_id": result.inserted_id,
                "student_id": student_id,
                "company_id": company["_id"],
                "opportunity_id": opportunity["_id"],
                "old_status": None,
                "new_status": fields["current_status"],
                "reason": "Application imported from pasted response sheet",
                "changed_by": None,
                "changed_by_role": "admin",
                "source": "response_paste",
                "created_at": now,
            })
        preview.append(entry)

    return serialize_mongo({
        "mode": "applied" if confirm else "preview",
        "company": company.get("name"),
        "role": opportunity.get("role"),
        "counts": counts,
        "rows": preview,
    })


# --------------------------------------------------------------------------
# shortlist
# --------------------------------------------------------------------------


async def import_shortlist(*, opportunity_id: str, raw_text: str, confirm: bool = False) -> dict:
    db = get_database()
    opportunity, company = await load_opportunity(db, opportunity_id)
    rows = read_shortlist_rows(raw_text)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No rows found. Paste the shortlist sheet contents.",
        )

    now = datetime.now(timezone.utc)
    preview: list[dict[str, Any]] = []
    # NOTE: a shortlist import never creates students. A shortlist sheet only
    # carries a name (and sometimes a resume link), which is not enough to make
    # a real, loginable person - and someone on a shortlist who never submitted
    # a response is a data problem to look at, not a record to invent. Rows with
    # no matching student are reported as unmatched instead.
    counts = {
        "rows": len(rows),
        "students_matched": 0,
        "matched_by_name": 0,
        "applications_to_mark": 0,
        "applications_to_create": 0,
        "ambiguous": 0,
        "unmatched": 0,
    }
    willing = {"interested": 0, "not_interested": 0, "no_response": 0}

    # Shortlist sheets often carry only a name, so fall back to matching against
    # the people who already applied to this opening.
    applicants = await build_applicant_name_index(db, opportunity["_id"])

    for index, cells in enumerate(rows, start=1):
        data = extract_shortlist_row(cells)
        willing[data["willing_to_join"] or "no_response"] += 1
        entry: dict[str, Any] = {
            "row": index,
            "name": data["name"],
            "email": data["email"],
            "phone": data["phone"],
            "willing_to_join": data["willing_to_join"],
        }

        student = await find_student(db, data)
        matched_via = "id" if student else None
        if not student:
            student, ambiguous = match_by_name(data["name"], applicants)
            if ambiguous:
                entry["action"] = "skip"
                entry["reason"] = f"Several applicants match the name '{data['name']}'. Add an email to the sheet."
                counts["ambiguous"] += 1
                preview.append(entry)
                continue
            if student:
                matched_via = "name"
                counts["matched_by_name"] += 1
        entry["matched_via"] = matched_via

        if not student:
            entry["action"] = "skip"
            entry["reason"] = (
                "No student on this opening matches this row. Import their response sheet first, "
                "or add an email/phone column so they can be matched."
            )
            counts["unmatched"] += 1
            preview.append(entry)
            continue

        counts["students_matched"] += 1
        entry["student_exists"] = True

        existing_application = await db[APPLICATIONS].find_one(
            {"opportunity_id": opportunity["_id"], "student_id": student["_id"]}
        )
        entry["action"] = "mark_shortlisted" if existing_application else "create_shortlisted"
        counts["applications_to_mark" if existing_application else "applications_to_create"] += 1

        if not confirm:
            preview.append(entry)
            continue

        # ---- write (existing students only) ----
        student_id = student["_id"]

        shortlist_sub = {
            "is_shortlisted": True,
            "resume": data["resume"],
            "call_date": data["call_date"],
            "call_status": data["call_status"],
            "willing_to_join": data["willing_to_join"],
            "willing_notes": data["willing_notes"],
            "source": "shortlist_paste",
            "imported_at": now,
        }

        existing_application = await db[APPLICATIONS].find_one(
            {"opportunity_id": opportunity["_id"], "student_id": student_id}
        )
        if existing_application:
            old_status = status_for_api(existing_application)
            # Being on the shortlist does not un-do a later interview or offer.
            new_status = _keep_status(existing_application, "SHORTLISTED")
            if existing_application.get("current_status") == "SHORTLISTED":
                new_status = "SHORTLISTED"
            await db[APPLICATIONS].update_one(
                {"_id": existing_application["_id"]},
                {"$set": {
                    "current_status": new_status,
                    "final_status": final_status_for(new_status, interested=True),
                    "shortlisted_at": now,
                    "shortlist": shortlist_sub,
                    "application_details.interested": True,
                    "updated_at": now,
                }},
            )
            application_id = existing_application["_id"]
        else:
            old_status = None
            new_status = "SHORTLISTED"
            document = {
                "student_id": student_id,
                "company_id": company["_id"],
                "opportunity_id": opportunity["_id"],
                "applied_at": None,
                "source": "shortlist_paste",
                "current_status": new_status,
                "final_status": final_status_for(new_status, interested=True),
                "application_details": build_application_details(
                    interested=True,
                    submitted_resume_url=data["resume"],
                    other_response={"shortlist_snapshot": data},
                ),
                "placement": default_placement(),
                "shortlist": shortlist_sub,
                "notes": None,
                "created_at": now,
                "updated_at": now,
            }
            application_id = (await db[APPLICATIONS].insert_one(document)).inserted_id

        if old_status != new_status:
            await db[STATUS_HISTORY].insert_one({
                "application_id": application_id,
                "student_id": student_id,
                "company_id": company["_id"],
                "opportunity_id": opportunity["_id"],
                "old_status": old_status,
                "new_status": new_status,
                "reason": "Marked shortlisted from pasted shortlist sheet",
                "notes": data["willing_notes"],
                "changed_by": None,
                "changed_by_role": "admin",
                "source": "shortlist_paste",
                "created_at": now,
            })
        preview.append(entry)

    return serialize_mongo({
        "mode": "applied" if confirm else "preview",
        "company": company.get("name"),
        "role": opportunity.get("role"),
        "counts": counts,
        "willing_breakdown": willing,
        "rows": preview,
    })
