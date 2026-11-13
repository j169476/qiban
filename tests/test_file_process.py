import pytest

from companion_core.errors import AssistantError
from companion_core.file_process import parse_file


def test_real_private_pipe_parser_preserves_unicode_and_line_locations():
    result = parse_file('中文.txt', '第一行🙂\n第二行'.encode(), guard=lambda: None)
    assert result['status'] == 'ok'
    assert result['units'] == [{'source': {'line': 1}, 'text': '第一行🙂'},
                               {'source': {'line': 2}, 'text': '第二行'}]


def test_original_name_is_format_metadata_not_a_path():
    result = parse_file('C:/no-such-folder/不存在.txt', b'actual supplied bytes', guard=lambda: None)
    assert result['units'][0]['text'] == 'actual supplied bytes'


def test_guard_cancellation_survives_child_cleanup():
    calls = []

    def guard():
        calls.append(1)
        if len(calls) > 1:
            raise AssistantError('test_cancelled', 'cancelled')

    with pytest.raises(AssistantError) as error:
        parse_file('sample.txt', b'sample', guard=guard)
    assert error.value.code == 'test_cancelled'


def test_timeout_is_explicit_not_a_successful_empty_document():
    # timeout=0 is the deterministic budget: Windows reports time.monotonic() through
    # GetTickCount64, whose tick is 15.6ms, so a deadline of "now + 1e-6" can still be in the
    # future on the very next reading and the parser would be given a poll window instead. A zero
    # budget is already due at the first check, so the child can never answer first.
    with pytest.raises(AssistantError) as error:
        parse_file('sample.txt', b'sample', guard=lambda: None, timeout=0)
    assert error.value.code == 'file_process_timeout'
