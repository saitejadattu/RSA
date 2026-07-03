from datetime import datetime, timezone

from app.config.constants import ROLE_STUDENT


def build_student_document(
    *,
    name: str,
    phone: str,
    email: str | None,
    stack: str | None,
    resume_link: str | None,
    password_hash: str,
    external_user_id: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    document = {
        "name": name,
        "external_user_id": external_user_id,
        "phone": phone,
        "stack": stack,
        "resume_link": resume_link,
        "password_hash": password_hash,
        "is_password_set": False,
        "force_password_reset": True,
        "role": ROLE_STUDENT,
        "created_at": now,
        "updated_at": now,
        "password_updated_at": None,
    }
    if email:
        document["email"] = email
    return document
