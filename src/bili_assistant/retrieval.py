"""Compatibility import; implementation lives in companion_core."""
import sys

from companion_core import retrieval as _implementation

sys.modules[__name__] = _implementation
