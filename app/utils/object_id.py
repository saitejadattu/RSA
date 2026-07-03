from bson import ObjectId
from bson.errors import InvalidId


def to_object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId as exc:
        raise ValueError("Invalid object id") from exc


def serialize_document(document: dict | None) -> dict | None:
    if document is None:
        return None
    serialized = dict(document)
    serialized["id"] = str(serialized.pop("_id"))
    return serialized
