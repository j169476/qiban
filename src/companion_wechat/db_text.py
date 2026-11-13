"""Bounded UTF-8 / single Zstandard frame decoding for diagnostic markers."""
from __future__ import annotations


class TextDecoderError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require_decoder():
    """Missing runtime support is an installation error, not absent metadata."""
    try:
        import zstandard
    except ModuleNotFoundError as exc:
        raise TextDecoderError("dependency_missing" if exc.name == "zstandard" else "dependency_unavailable") from None
    except (ImportError, OSError):
        raise TextDecoderError("dependency_unavailable") from None
    if zstandard.__version__ != "0.25.0":
        raise TextDecoderError("dependency_version_unsupported")
    return zstandard


def sqlite_text(value):
    """WCDB can store compressed bytes in a column declared as TEXT."""
    try:
        return value.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return value


def short_text(value):
    if isinstance(value, str):
        return value if len(value.encode("utf-8", "surrogatepass")) <= 4096 else None
    if not isinstance(value, bytes) or len(value) > 4096:
        return None
    if value.startswith(b"\x28\xb5\x2f\xfd"):
        zstandard = require_decoder()
        try:
            header = zstandard.get_frame_parameters(value)
            if header.window_size > 1024 * 1024 or header.content_size not in (
                    zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR) and header.content_size > 4096:
                return None
            value = zstandard.ZstdDecompressor(max_window_size=1024).decompress(
                value, max_output_size=4096, allow_extra_data=False)
        except Exception:
            return None
    if len(value) > 4096:
        return None
    try:
        return value.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None
