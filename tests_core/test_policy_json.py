import json

import pytest

from companion_core.errors import AssistantError
from companion_core.policy_json import PolicyJSON


def test_policy_sequences_are_immutable_and_depend_on_complete_text():
    parsed = PolicyJSON()
    first = parsed.sequence('["owner-a"]')
    assert parsed.sequence('["owner-a"]') is first
    assert parsed.sequence('["owner-b"]') == ("owner-b",)
    pins = parsed.sequence('[["event/a","v1"]]', pairs=True)
    assert parsed.sequence('[["event/a","v2"]]', pairs=True) == (("event/a", "v2"),)
    with pytest.raises(TypeError):
        pins[0][1] = "changed"
    assert parsed.sequence('[["event/a","v1"]]', pairs=True) is pins


def test_policy_parsing_retention_is_bounded_and_oversized_values_are_not_retained():
    parsed = PolicyJSON(max_items=2, max_bytes=4096)
    for index in range(100):
        assert parsed.sequence(json.dumps([str(index)])) == (str(index),)
        assert len(parsed._values) <= 2 and parsed._bytes <= 4096
    before = len(parsed._values), parsed._bytes
    large = "x" * 70_000
    assert parsed.sequence(json.dumps([large])) == (large,)
    assert (len(parsed._values), parsed._bytes) == before


@pytest.mark.parametrize("raw,pairs", [('{}', False), ('"owner"', False), ('[42]', False),
                                      ('[["source"]]', True), ('[["source",42]]', True)])
def test_malformed_policy_sequences_fail_closed(raw, pairs):
    with pytest.raises(AssistantError, match="来源字段"):
        PolicyJSON().sequence(raw, pairs=pairs)
