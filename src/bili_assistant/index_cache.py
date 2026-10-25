"""Compatibility import; implementation lives in companion_core."""
import sys

from companion_core import index_cache as _implementation

sys.modules[__name__] = _implementation
