"""Bounded YAML preview for an existing, explicitly selected admin record."""
import json

import yaml

from companion_core.errors import AssistantError
from companion_core.storage import StrictLoader

MAX_BYTES = 24_000


def _encoded(text):
    """UTF-8 bytes of the text, refusing unpaired surrogates instead of raising."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise AssistantError("yaml_import", "YAML 文件含有无法保存的字符，请重新导出后再导入") from None


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate normalized key')
        value[key] = item
    return value


def parse(text):
    if not isinstance(text, str):
        raise AssistantError("yaml_import", "YAML 文件不得超过 24 KB")
    if len(_encoded(text)) > MAX_BYTES:
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
        # JSON and YAML spell the same values, so one validated JSON round trip rejects
        # YAML-only types, dates and non-finite numbers, and merges escaped surrogate pairs
        # such as "\uD83D\uDE42" into the single character they stand for. Duplicate keys,
        # aliases and anchors stay rejected by the strict loader and the event scan above.
        value = json.loads(json.dumps(value, allow_nan=False), object_pairs_hook=_unique_object)
        _encoded(json.dumps(value, ensure_ascii=False))  # Reject unpaired surrogate escapes.
        return value
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        raise AssistantError("yaml_import", "YAML 无效：仅支持无重复键、无引用且符合资料结构的对象") from None
