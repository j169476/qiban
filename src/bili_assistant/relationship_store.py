"""Compatibility import; implementation lives in companion_core."""
import sys

from companion_core import relationship_store as _implementation

sys.modules[__name__] = _implementation
