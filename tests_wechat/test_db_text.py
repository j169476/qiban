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
