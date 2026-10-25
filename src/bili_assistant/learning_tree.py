"""Compatibility import for the core knowledge tree."""
import sys

from companion_core import learning_tree as _implementation

sys.modules[__name__] = _implementation
