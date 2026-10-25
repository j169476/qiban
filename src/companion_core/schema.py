"""Common contracts. Platform profiles supply additional document schemas."""

import json
from pathlib import Path

from .errors import AssistantError

MEMORY_COLLECTIONS = ("memories", "knowledge", "relationships", "diaries", "skills", "interest_signals")


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": False}


TEXT = {"type": "string", "maxLength": 2000}
REF = obj({"platform": {"type": "string", "minLength": 1, "maxLength": 80},
           "scope": {"type": "string", "maxLength": 200},
           "user_id": {"type": "string", "minLength": 1, "maxLength": 200}})
EVIDENCE = obj({"event_id": TEXT, "version": TEXT, "quote": TEXT})
IDENTITY_SCHEMAS = {
    "identity_links": obj({
        "schema_version": {"const": 1}, "id": TEXT,
        "left": REF, "right": REF,
        "status": {"enum": ["suspected", "hard", "disputed", "revoked"]},
        "reason": TEXT, "evidence": {"type": "array", "items": EVIDENCE, "maxItems": 30},
        "history": {"type": "array", "items": {"type": "object"}, "maxItems": 100},
        "manual": {"type": "boolean"}, "locked": {"type": "boolean"},
    }),
}
# Invalidate parsed-record caches when semantic validation changes as well.
IDENTITY_SCHEMAS["identity_links"]["$comment"] = "Cross-platform distinct endpoints; semantic validation v2."


def validate_identity_link(data):
    """JSON Schema cannot express equality between two sibling properties."""
    if data["left"]["platform"] == data["right"]["platform"]:
        raise AssistantError("invalid_data", "identity_links: 关联两端必须属于不同平台，不能关联同一主体")
    return data

IDENTITY_PROPOSALS = {"type": "array", "maxItems": 5, "items": obj({
    "left": REF, "right": REF, "kind": {"enum": ["claim", "correspondence", "contradiction"]},
    "reason": {"type": "string", "minLength": 8, "maxLength": 2000},
    "specific_correspondence": {"type": "boolean"},
    "evidence": {"type": "array", "items": EVIDENCE, "minItems": 1, "maxItems": 6},
})}


_ASSESSMENT = json.loads((Path(__file__).parent / "assessment_schemas.json").read_text(encoding="utf-8"))
EMOTION_CHANGE = _ASSESSMENT["EMOTION_CHANGE"]
ANSWER = _ASSESSMENT["ANSWER"]
GRADE = _ASSESSMENT["GRADE"]
QUESTION = _ASSESSMENT["QUESTION"]
