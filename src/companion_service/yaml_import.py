"""Bounded YAML preview for an existing, explicitly selected admin record."""
import json

import yaml

from companion_core.errors import AssistantError
from companion_core.storage import StrictLoader

MAX_BYTES = 24_000


def parse(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
        raise AssistantError("yaml_import", "YAML 文件不得超过 24 KB")
    try:
        depth = nodes = 0
        for event in yaml.parse(text, Loader=StrictLoader):
            nodes += 1
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise ValueError()
            if isinstance(event, (yaml.SequenceStartEvent, yaml.MappingStartEvent)):
                depth += 1
            elif isinstance(event, (yaml.SequenceEndEvent, yaml.MappingEndEvent)):
                depth -= 1
            if nodes > 20_000 or depth > 32:
                raise ValueError()
        value = yaml.load(text, Loader=StrictLoader)
        if not isinstance(value, dict):
            raise ValueError()
        json.dumps(value, allow_nan=False)  # Reject YAML-only types, dates and non-finite values.
        return value
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        raise AssistantError("yaml_import", "YAML 无效：仅支持无重复键、无引用且符合资料结构的对象") from None
