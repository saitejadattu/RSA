from datetime import datetime
from typing import Any

from bson import ObjectId


def serialize_mongo(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [serialize_mongo(item) for item in value]
    if isinstance(value, dict):
        serialized = {}
        for key, item in value.items():
            output_key = "id" if key == "_id" else key
            serialized[output_key] = serialize_mongo(item)
        return serialized
    return value
