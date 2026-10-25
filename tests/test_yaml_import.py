import pytest

from companion_core.errors import AssistantError
from companion_service.yaml_import import parse


@pytest.mark.parametrize("text", ["a: 1\na: 2", "a: &loop [*loop]", "a: .inf", "a: 2026-10-23",
                                "[]", "a: !!python/object:invalid {}", "[" * 40 + "]" * 40, "a: " + "x" * 256_000],
                         ids=["duplicate", "cycle", "infinity", "date", "array", "tag", "depth", "size"])
def test_yaml_preview_rejects_unsafe_or_non_json_data(text):
    with pytest.raises(AssistantError):
        parse(text)


def test_yaml_preview_preserves_text_without_executing_it():
    assert parse('content: "<script>ignored</script>"\ncount: 2') == {"content": "<script>ignored</script>", "count": 2}
