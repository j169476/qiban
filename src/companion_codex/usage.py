"""Read only allowlisted numeric fields from Codex JSONL turn completion events."""
import json

from companion_core.observability import TOKENS


def from_trace(path):
    values = {key: [] for key in TOKENS}
    count = 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace") if path.stat().st_size <= 8_000_000 else ""
    except OSError:
        text = ""
    if not text:
        return {**{key: None for key in TOKENS}, "usage_events": 0}
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        count += 1
        for key in TOKENS:
            number = usage.get(key)
            values[key].append(number if type(number) is int and 0 <= number <= 10**12 else None)
    result = {key: sum(items) if items and all(v is not None for v in items) else None
              for key, items in values.items()}
    for part, total in (("cached_input_tokens", "input_tokens"), ("reasoning_output_tokens", "output_tokens")):
        if result[part] is not None and result[total] is not None and result[part] > result[total]:
            result[part] = None
    return {**result, "usage_events": count}
