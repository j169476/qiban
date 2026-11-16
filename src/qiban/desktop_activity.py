"""Live task scopes, removed on success, exception and cancellation."""
import threading
import time
from contextlib import contextmanager


class Activity:
    def __init__(self):
        self.lock = threading.RLock()
        self.items = {}

    @contextmanager
    def track(self, platform, state):
        token = object()
        with self.lock:
            self.items[token] = dict(platform=platform, state=state, started=time.time())
        try:
            yield
        finally:
            with self.lock:
                self.items.pop(token, None)

    def snapshot(self):
        with self.lock:
            rows = [dict(row) for row in self.items.values()]
        priority = {'replying': 0, 'learning': 1, 'watching': 2, 'browsing': 3}
        return sorted(rows, key=lambda row: (priority.get(row['state'], 4), row['started']))
