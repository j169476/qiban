"""Small DB-API compatibility port; SQL remains parameterized and inspectable.

Only parameter markers and explicit legacy transaction-start semantics differ.
This is not a general SQL rewriter. Unsupported SQLite statements are rejected.
"""
from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from functools import lru_cache
from types import MappingProxyType

from .errors import AssistantError


class Row:
    def __init__(self, names, values, index=None):
        self.names, self.values = names, values
        self.index = index if index is not None else MappingProxyType({name: i for i, name in enumerate(names)})

    def keys(self):
        return self.names

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)

    def __getitem__(self, key):
        return self.values[self.index[key] if isinstance(key, str) else key]


def row_factory(cursor):
    names = tuple(column.name for column in cursor.description) if cursor.description else ()
    index = MappingProxyType({name: i for i, name in enumerate(names)})
    return lambda values: Row(names, values, index)


def parameters(sql):
    """Convert only unquoted qmark parameters; preserve literal % and question marks."""
    return _cached_parameters(sql) if len(sql) <= 8192 else _parameters(sql)


@lru_cache(maxsize=256)
def _cached_parameters(sql):
    # Only statement templates are cached, never their bound arguments.
    return _parameters(sql)


def _parameters(sql):
    result, quoted, offset = [], None, 0
    while offset < len(sql):
        char = sql[offset]
        if quoted:
            result.append("%%" if char == "%" else char)
            if char == quoted:
                if offset + 1 < len(sql) and sql[offset + 1] == quoted:
                    result.append(quoted)
                    offset += 1
                else:
                    quoted = None
        elif char in ("'", '"'):
            quoted = char
            result.append(char)
        else:
            result.append("%s" if char == "?" else "%%" if char == "%" else char)
        offset += 1
    if quoted:
        raise AssistantError("storage_sql", "SQL 字符串未闭合")
    return "".join(result)


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def __iter__(self):
        return iter(self.cursor)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    @property
    def rowcount(self):
        return self.cursor.rowcount


class Connection:
    dialect = "postgres"

    def __init__(self, connection, advisory_key):
        self.connection, self.advisory_key = connection, advisory_key

    def execute(self, sql, arguments=()):
        normalized = " ".join(sql.split()).upper()
        if normalized == "BEGIN IMMEDIATE":
            return Cursor(self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (self.advisory_key,)))
        if normalized.startswith(("PRAGMA ", "INSERT OR ")):
            raise AssistantError("storage_sql", "尚未迁移的 SQLite 语句不能在正式数据库执行")
        return Cursor(self.connection.execute(parameters(sql), arguments))

    def executemany(self, sql, arguments):
        cursor = self.connection.cursor()
        cursor.executemany(parameters(sql), arguments)
        return Cursor(cursor)

    def executescript(self, sql):
        # Only checked-in migration SQL, never text supplied by a model or web form.
        return Cursor(self.connection.execute(sql, prepare=False))


class Database:
    def __init__(self, connection_options, *, namespace="companion"):
        from psycopg_pool import ConnectionPool

        # Deliberately do not implement __repr__ for options containing a password.
        self._options = dict(connection_options)
        self._local = threading.local()
        self.key = int.from_bytes(hashlib.sha256(namespace.encode()).digest()[:8], "big", signed=True)
        self._pool = ConnectionPool(kwargs={**self._options, "row_factory": row_factory, "connect_timeout": 5},
                                    min_size=0, max_size=4, max_waiting=16, timeout=10, open=False,
                                    name="companion-database")
        self._pool_lock = threading.Lock()
        self._opened = False

    def close(self):
        self._pool.close()

    @contextmanager
    def connect(self):
        import psycopg

        previous = getattr(self._local, "connection", None)
        if previous is not None:
            # A nested repository write joins the same unit of work. Savepoints
            # prevent partial mutations when an inner operation fails.
            with previous.transaction():
                yield Connection(previous, self.key)
            return
        try:
            with self._pool_lock:
                if not self._opened:
                    self._pool.open()
                    self._opened = True
            with self._pool.connection() as connection:
                self._local.connection = connection
                try:
                    # Reset both limits in one round trip, using SET so a caller
                    # can still select transaction isolation before its first query.
                    # Graph deadlines still override statement_timeout locally.
                    connection.execute("SET statement_timeout='15s'; SET lock_timeout='5s'", prepare=False)
                    yield Connection(connection, self.key)
                finally:
                    self._local.connection = None
        except psycopg.Error:
            # Never forward connection strings, SQL values or driver diagnostics.
            raise AssistantError("database_error", "数据库操作未完成；请检查本机数据库状态与版本冲突") from None
