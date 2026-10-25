"""Optional synthetic capacity diagnostics. Never retain SQL values or payloads."""
import hashlib
import inspect
import re
import time
from collections import defaultdict
from functools import wraps

import psycopg


class CapacityTimings:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.functions = defaultdict(lambda: [0, 0.0, 0.0])
        self.sql = defaultdict(lambda: [0, 0.0, 0.0])
        self.query_labels = {}
        self.saved = []

    @staticmethod
    def record(target, key, started):
        elapsed = time.perf_counter() - started
        row = target[key]
        row[0] += 1
        row[1] += elapsed
        row[2] = max(row[2], elapsed)

    def wrap(self, owner, name):
        original = getattr(owner, name)
        label = owner.__name__ + "." + name
        if inspect.iscoroutinefunction(original):
            @wraps(original)
            async def wrapped(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return await original(*args, **kwargs)
                finally:
                    self.record(self.functions, label, started)
        else:
            @wraps(original)
            def wrapped(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    self.record(self.functions, label, started)
        self.saved.append((owner, name, original))
        setattr(owner, name, wrapped)

    def __enter__(self):
        if not self.enabled:
            return self
        from companion_core import provenance
        from companion_core.access import AccessService
        from companion_core.governance import GovernanceRepository, GovernedContext
        from companion_core.interaction_growth import InteractionGrowth
        from companion_core.postgres import PostgresStore
        from companion_core.runtime import Companion

        for owner, names in (
            (AccessService, ("_lease", "_walk", "readable_snapshot")),
            (GovernanceRepository, ("disclosure_snapshot", "readable_resources")),
            (GovernedContext, ("check", "conversation", "freeze", "classify_written")),
            (PostgresStore, ("read", "write", "records_selected")),
            (Companion, ("governed_memory", "dispatch")),
            (InteractionGrowth, ("context", "check", "save")),
            (provenance, ("freeze", "reduce_pins", "rebase")),
        ):
            for name in names:
                self.wrap(owner, name)
        for name in ("execute", "executemany"):
            original = getattr(psycopg.Cursor, name)

            def timed(cursor, query, *args, _name=name, _original=original, **kwargs):
                # A bounded cache of trusted statement templates, not parameters.
                text = query if isinstance(query, str) else str(query)
                key = self.query_labels.get(text)
                if key is None:
                    tokens = re.findall(r"[A-Za-z_]+", text)
                    kind = tokens[0].upper() if tokens else "SQL"
                    key = _name + ":" + kind + ":" + hashlib.sha256(text.encode()).hexdigest()[:16]
                    if key not in self.sql and len(self.sql) >= 256:
                        key = "other_statements"
                    if len(self.query_labels) < 256 and len(text) <= 8192:
                        self.query_labels[text] = key
                started = time.perf_counter()
                try:
                    return _original(cursor, query, *args, **kwargs)
                finally:
                    self.record(self.sql, key, started)

            self.saved.append((psycopg.Cursor, name, original))
            setattr(psycopg.Cursor, name, timed)
        return self

    def __exit__(self, *args):
        for owner, name, original in reversed(self.saved):
            setattr(owner, name, original)

    def report(self):
        def rows(values):
            return {key: {"calls": row[0], "seconds": round(row[1], 6), "max_seconds": round(row[2], 6)}
                    for key, row in sorted(values.items(), key=lambda item: -item[1][1])}

        return {"enabled": self.enabled, "nested_times_are_not_additive": True,
                "functions": rows(self.functions), "sql": rows(self.sql)}
