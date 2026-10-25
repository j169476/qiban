"""Compatibility import; implementation lives in companion_core."""
import sys

from companion_core import errors as _implementation

sys.modules[__name__] = _implementation
