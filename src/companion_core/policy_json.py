"""Bounded parsing reuse for freshly read policy JSON, never authorization results."""
import json
import sys
import threading
from collections import OrderedDict

from .errors import AssistantError


class PolicyJSON:
    """Store-local, immutable sequences keyed by their complete database text.

    Every disclosure still reads current rows, builds fresh policies and validates
    the complete graph. Versions alone are not cache keys: a changed field with an
    unchanged version must be observed too. Requests, grants, expiry decisions and
    readable/not-readable results are never retained here.
    """
    def __init__(self, *, max_items=16_384, max_bytes=16 * 1024 * 1024):
        self.max_items, self.max_bytes = max_items, max_bytes
        self._values = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    def sequence(self, raw, *, pairs=False):
        key = (pairs, raw)
        with self._lock:
            found = self._values.get(key)
            if found is not None:
                self._values.move_to_end(key)
                return found[0]
        data = json.loads(raw)
        valid = isinstance(data, list) and all(
            isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item)
            if pairs else isinstance(item, str) for item in data)
        if not valid:
            raise AssistantError("record_policy", "资料权限的来源字段无效")
        value = tuple(tuple(item) for item in data) if pairs else tuple(data)
        weight = sys.getsizeof(key) + sys.getsizeof(raw) + sys.getsizeof(value) + 256
        weight += sum(sys.getsizeof(item) for item in value)
        if pairs:
            weight += sum(sys.getsizeof(part) for item in value for part in item)
        # Oversized legacy metadata remains readable; it is simply not retained.
        if len(raw) > 65_536 or weight > self.max_bytes or self.max_items < 1:
            return value
        with self._lock:
            found = self._values.get(key)
            if found is not None:
                self._values.move_to_end(key)
                return found[0]
            while self._values and (len(self._values) >= self.max_items or self._bytes + weight > self.max_bytes):
                _, (_, removed) = self._values.popitem(last=False)
                self._bytes -= removed
            self._values[key] = (value, weight)
            self._bytes += weight
        return value
