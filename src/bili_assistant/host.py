"""Compatibility import; implementation lives in companion_codex."""
import sys

from companion_codex import host as _implementation

sys.modules[__name__] = _implementation
