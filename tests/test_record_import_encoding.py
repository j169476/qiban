"""Advanced import parser: escaped surrogate pairs and UTF-8 safety."""
import json

import pytest

from companion_core.errors import AssistantError
from companion_core.record_import import MAX_BYTES, parse

EMOJI = "\U0001f642"
WIDE = "\U00020bb7"


def rejected(text):
    with pytest.raises(AssistantError) as raised:
        parse(text)
    assert raised.value.code == "yaml_import"
    return str(raised.value)


def test_actual_and_escaped_emoji_become_the_same_value():
    assert parse(r'{"content":"\uD83D\uDE42"}') == {"content": EMOJI}
    assert parse('content: "🙂"') == {"content": EMOJI}


def test_escaped_emoji_result_can_be_encoded_again():
    value = parse(r'{"content":"\uD83D\uDE42"}')
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert encoded == '{"content": "🙂"}'.encode("utf-8")


@pytest.mark.parametrize(("text", "expected"), [
    (r'{"title":"\u65E5\u672C\u8A9E"}', {"title": "日本語"}),
    (r'{"note":"\u4E2D\u6587"}', {"note": "中文"}),
    (r'{"wide":"\uD842\uDFB7"}', {"wide": WIDE}),
], ids=["japanese", "chinese", "non_bmp"])
def test_escaped_text_written_by_dotnet_normalizes_to_one_character(text, expected):
    assert parse(text) == expected


def test_nested_reference_and_tag_values_normalize():
    text = (r'{"content":"\uD83D\uDE42","tags":["\u65E5","\uD842\uDFB7"],'
            r'"source":{"type":"conversation","reference":"\uD83D\uDE42"}}')
    assert parse(text) == {"content": EMOJI, "tags": ["日", WIDE],
                           "source": {"type": "conversation", "reference": EMOJI}}


@pytest.mark.parametrize(("text", "expected"), [
    ("content: '\\uD83D\\uDE42'", {"content": "\\uD83D\\uDE42"}),
    (r'{"content":"\\uD83D\\uDE42"}', {"content": "\\uD83D\\uDE42"}),
], ids=["single_quoted", "double_escaped_json"])
def test_literal_double_escaped_text_stays_literal(text, expected):
    assert parse(text) == expected


@pytest.mark.parametrize("text", [r'{"content":"\uD83D"}', r'{"content":"\uDE42"}'], ids=["high", "low"])
def test_unpaired_surrogate_escape_is_rejected(text):
    rejected(text)


def test_raw_text_surrogate_is_a_controlled_error():
    rejected('content: "' + "\ud83d" + '"')


@pytest.mark.parametrize("text", [
    "a: 1\na: 2",
    "a: &anchor 1\nb: *anchor",
    "a: " + "[" * 40 + "]" * 40,
    "content: " + "x" * MAX_BYTES,
], ids=["duplicate", "alias", "depth", "oversize"])
def test_existing_limits_still_reject(text):
    rejected(text)


def test_normal_documents_keep_their_json_types():
    text = 'content: "正文"\ncount: 2\nflag: true\nratio: 1.5\nempty: null'
    assert parse(text) == {"content": "正文", "count": 2, "flag": True, "ratio": 1.5, "empty": None}


def test_normalization_does_not_hide_duplicate_unicode_keys():
    rejected(r'{"🙂":1,"\uD83D\uDE42":2}')
    rejected(r'{"source":{"𠮷":"first","\uD842\uDFB7":"second"}}')
