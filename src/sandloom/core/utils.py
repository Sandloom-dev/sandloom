import json
import uuid
from typing import Any


def generate_id() -> str:
    return str(uuid.uuid4())


def safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
