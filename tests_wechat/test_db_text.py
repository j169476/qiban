"""Small synthetic marker frames only; optional native decoder in probe venv."""
import pytest

from companion_wechat.db_probe import marker_number
from companion_wechat.db_text import short_text, sqlite_text


@pytest.mark.parametrize("value,expected", [(b"plain", "plain"), (b"\x80", b"\x80"),
                                           ("文字".encode(), "文字")])
def test_sqlite_text_preserves_binary(value, expected):
    assert sqlite_text(value) == expected


@pytest.mark.parametrize("value", [None, 1, b"\x80", b"a" * 4097, "字" * 1366])
def test_unknown_or_oversized_input_rejected(value):
    assert short_text(value) is None


@pytest.mark.parametrize("sized", [True, False])
def test_small_compressed_marker(sized):
    zstd = pytest.importorskip("zstandard")
    frame = zstd.ZstdCompressor(write_content_size=sized).compress(b"XLD-1234ABCD-P-01")
    assert marker_number(frame, "XLD-1234ABCD", "owner_main", "synthetic") == 1


@pytest.mark.parametrize("kind", ["bomb", "truncated", "extra", "unknown_bomb"])
def test_invalid_or_oversized_frames_rejected(kind):
    zstd = pytest.importorskip("zstandard")
    payload = b"a" * 100000 if "bomb" in kind else b"marker"
    frame = zstd.ZstdCompressor(write_content_size=kind != "unknown_bomb").compress(payload)
    if kind == "truncated":
        frame = frame[:-1]
    if kind == "extra":
        frame += b"extra"
    assert short_text(frame) is None


def test_missing_decoder_is_not_reported_as_absent_text_or_mention(monkeypatch):
    import sys

    from companion_wechat.db_text import TextDecoderError, require_decoder

    monkeypatch.setitem(sys.modules, "zstandard", None)
    for call in (require_decoder, lambda: short_text(b"\x28\xb5\x2f\xfd\x00")):
        with pytest.raises(TextDecoderError) as caught:
            call()
        assert caught.value.code == "dependency_missing"


def test_compressed_mention_metadata_uses_exact_account_identity():
    import zstandard

    from companion_wechat.db_triggers import message

    meta = zstandard.ZstdCompressor().compress(b'<msgsource><atuserlist><![CDATA[wxid_test]]></atuserlist></msgsource>')
    assert message(1, "@display-name hello", meta, "wxid_test", own_reference=lambda _: False)[1] == "mention"
    assert message(1, "@display-name hello", meta, "wxid_other", own_reference=lambda _: False)[1] == "none"


@pytest.mark.parametrize("entry", ["source", "probe"])
def test_native_start_checks_decoder_before_opening_wechat(tmp_path, monkeypatch, entry):
    from companion_wechat import db_probe, db_source, db_text, db_windows

    def unavailable():
        raise db_text.TextDecoderError("dependency_missing")

    def forbidden(**kwargs):
        pytest.fail("native client must not be opened before dependency validation")

    monkeypatch.setattr(db_text, "require_decoder", unavailable)
    monkeypatch.setattr(db_windows, "discover_wechat", forbidden)
    config = {"account_dirs": [str(tmp_path)], "target_hints": [{"kind": "private", "hint": "test"}]}
    prepare = db_source._native_prepare(config, None) if entry == "source" else db_probe.prepare(config)
    with pytest.raises(db_text.TextDecoderError) as caught:
        with prepare:
            pytest.fail("missing decoder cannot establish a source baseline")
    assert caught.value.code == "dependency_missing"
