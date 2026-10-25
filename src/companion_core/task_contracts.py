"""Explicit private tasks, independent of the hosting UI and platform adapters."""
from .schema import REF, obj


def text(size=2000, minimum=1):
    return {"type": "string", "minLength": minimum, "maxLength": size}


REQUESTS = {
    "web": obj({"prompt": text()}),
    "image": obj({"prompt": text()}),
    "learn": obj({"objective": text(), "criteria": {"type": "array", "minItems": 1, "maxItems": 6,
                  "items": text(500)}, "round_limit": {"type": "integer", "minimum": 1, "maximum": 6}}),
    "video": obj({"bvid": {"type": "string", "pattern": "^BV[0-9A-Za-z]{10}$"},
                  "page_index": {"type": "integer", "minimum": 0, "maximum": 999},
                  "start_seconds": {"type": "number", "minimum": 0, "maximum": 86400},
                  "end_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 86400},
                  "mode": {"enum": ["text", "visual"]}, "reason": text()}),
}
SCHEMAS = {
    "task_requests": obj({"schema_version": {"const": 1}, "id": text(200), "actor": REF,
        "conversation": text(400), "kind": {"enum": list(REQUESTS)}, "arguments": {"type": "object"},
        "source": obj({"type": {"const": "user_instruction"}, "recorded_at": text(100), "reference": text(200)}),
        "locked": {"type": "boolean"}}),
    "task_results": obj({"schema_version": {"const": 1}, "id": text(200), "task_id": text(200),
        "step": {"type": "integer", "minimum": 1, "maximum": 100}, "phase": text(80),
        "payload": {"type": "object"}, "summary": text(12000, 0), "complete": {"type": "boolean"},
        "source": obj({"type": {"const": "task"}, "recorded_at": text(100), "reference": text(200)}),
        "locked": {"type": "boolean"}}),
    "task_assets": obj({"schema_version": {"const": 1}, "id": text(200), "task_id": text(200),
        "filename": {"type": "string", "pattern": "^[a-f0-9]{64}\\.(png|jpg|webp)$"},
        "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "locked": {"type": "boolean"}}),
}
