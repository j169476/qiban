"""Bounded SQLCipher reader for an existing Windows database and its WAL.

This module never discovers accounts, obtains keys, or saves decrypted pages.
Only active WAL databases with existing sidecars are accepted. SQLCipher owns
transaction/locking semantics; do not use immutable=1 on a live database.
Run in a dedicated reader process: SQLite can reuse a writable SHM mapping if
another SQLCipher connection in the same process has already opened the file.
"""

from __future__ import annotations

import ctypes
import math
import os
import time
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path

SQLCIPHER_PACKAGE_VERSION = "0.6.4"
DEFAULT_QUERY_TIMEOUT = 2.0
CACHE_KIB = 2048
_CODES = {
    "unsupported_platform", "path_invalid", "source_unavailable", "sidecars_unavailable",
    "key_invalid", "dependency_missing", "dependency_version_unsupported", "dependency_unavailable",
    "database_unreadable", "database_busy", "query_denied", "query_timeout", "query_failed",
    "connection_closed", "configuration_invalid",
}


class DatabaseReadError(Exception):
    """A fixed public code; never include paths, SQL, keys, or database content."""

    def __init__(self, code: str):
        self.code = code if code in _CODES else "query_failed"
        super().__init__(self.code)


def _source_path(value) -> Path:
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or "://" in raw or raw.startswith(("\\\\", "//")):
            raise DatabaseReadError("path_invalid")
        path = Path(raw).resolve(strict=True)
        if str(path).startswith(("\\\\", "//")) or path.suffix.lower() != ".db" or not path.is_file():
            raise DatabaseReadError("path_invalid")
        if any(":" in part for part in path.parts[1:]):
            raise DatabaseReadError("path_invalid")
        return path
    except DatabaseReadError:
        raise
    except Exception:
        raise DatabaseReadError("path_invalid") from None


@contextmanager
def _pin_sources(path: Path):
    """Keep OPEN_EXISTING read handles without FILE_SHARE_DELETE until close.

Windows SQLite's readonly_shm path still calls OPEN_ALWAYS. Pinning all three
existing files prevents deletion between validation and SQLite opening them.
Missing/empty sidecars are deliberately unsupported, including inactive shards.
    """
    if os.name != "nt":
        raise DatabaseReadError("unsupported_platform")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    kernel.GetFileSizeEx.restype = wintypes.BOOL
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    if kernel.GetDriveTypeW(path.anchor) not in (2, 3):  # Removable/fixed local drives only.
        raise DatabaseReadError("path_invalid")
    handles = []
    try:
        for suffix, minimum in (("", 4096), ("-wal", 32), ("-shm", 32768)):
            handle = kernel.CreateFileW(str(path) + suffix, 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                raise DatabaseReadError("sidecars_unavailable" if suffix else "source_unavailable")
            handles.append(handle)
            size = ctypes.c_longlong()
            if not kernel.GetFileSizeEx(handle, ctypes.byref(size)) or size.value < minimum:
                raise DatabaseReadError("sidecars_unavailable" if suffix else "source_unavailable")
        yield
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def _load_sqlcipher():
    try:
        if metadata.version("sqlcipher3") != SQLCIPHER_PACKAGE_VERSION:
            raise DatabaseReadError("dependency_version_unsupported")
        import sqlcipher3
    except metadata.PackageNotFoundError:
        raise DatabaseReadError("dependency_missing") from None
    except (ImportError, OSError):
        raise DatabaseReadError("dependency_unavailable") from None
    # This exact Windows wheel was validated for readonly_shm and raw-key APIs.
    if sqlcipher3.sqlite_version != "3.53.4":
        raise DatabaseReadError("dependency_version_unsupported")
    return sqlcipher3


def _key_literal(raw_key) -> bytearray:
    if not isinstance(raw_key, (bytes, bytearray, memoryview)) or len(raw_key) != 32:
        raise DatabaseReadError("key_invalid")
    literal = bytearray(67)
    literal[0:2] = b"x'"
    literal[-1] = ord("'")
    digits = b"0123456789abcdef"
    try:
        for index, value in enumerate(raw_key):
            literal[2 + index * 2] = digits[value >> 4]
            literal[3 + index * 2] = digits[value & 15]
    except Exception:
        literal[:] = b"\x00" * len(literal)
        raise DatabaseReadError("key_invalid") from None
    return literal


class _ReadOnlyCursor:
    def __init__(self, owner, cursor, deadline):
        self._owner = owner
        self._cursor = cursor
        self._deadline = deadline

    @property
    def description(self):
        return self._owner._call(lambda: self._cursor.description, self._deadline)

    def fetchone(self):
        return self._owner._call(self._cursor.fetchone, self._deadline)

    def fetchmany(self, size=100):
        if type(size) is not int or not 0 < size <= 10_000:
            raise DatabaseReadError("configuration_invalid")
        return self._owner._call(lambda: self._cursor.fetchmany(size), self._deadline)

    def fetchall(self):
        return self._owner._call(self._cursor.fetchall, self._deadline)

    def close(self):
        return self._owner._call(self._cursor.close, None)

    def __iter__(self):
        return self

    def __next__(self):
        result = self.fetchone()
        if result is None:
            raise StopIteration
        return result


class ReadOnlyConnection:
    """Small query-only facade; no tracing, extension loading, backup, or attach.

Rows are SQLCipher Row objects (index or column-name access). Each execute and
its fetches share one deadline. Explicit BEGIN / ROLLBACK delimit snapshots.
The caller must keep transactions short to avoid delaying writer checkpoints.
    """

    def __init__(self, connection, driver, query_timeout: float):
        self._connection = connection
        self._driver = driver
        self._query_timeout = query_timeout
        self._deadline = None
        self._closed = False
        self._denied = False
        self._timed_out = False
        connection.set_progress_handler(self._progress, 1000)
        connection.set_authorizer(self._authorize)

    def _progress(self):
        expired = self._deadline is not None and time.monotonic() >= self._deadline
        self._timed_out = self._timed_out or expired
        return int(expired)

    def _authorize(self, action, first, second, database, trigger):
        driver = self._driver
        allowed = action in {
            driver.SQLITE_SELECT, driver.SQLITE_READ, driver.SQLITE_TRANSACTION,
            getattr(driver, "SQLITE_RECURSIVE", 33),
        }
        if action == driver.SQLITE_FUNCTION:
            allowed = (second or "").lower() in {"max", "coalesce", "length", "like", "count", "sum"}
        elif action == driver.SQLITE_PRAGMA:
            name = (first or "").lower()
            allowed = name in {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo"}
            allowed = allowed or (second is None and name in {
                "query_only", "temp_store", "cache_size", "cipher_memory_security", "cipher_version",
                "journal_mode", "page_size", "page_count", "schema_version", "database_list",
            })
        if not allowed:
            self._denied = True
        return driver.SQLITE_OK if allowed else driver.SQLITE_DENY

    def _call(self, function, deadline):
        if self._closed:
            raise DatabaseReadError("connection_closed")
        self._deadline = deadline
        self._denied = self._timed_out = False
        if deadline is not None and time.monotonic() >= deadline:
            raise DatabaseReadError("query_timeout")
        try:
            return function()
        except Exception as exc:
            if self._timed_out:
                code = "query_timeout"
            elif self._denied:
                code = "query_denied"
            elif (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF in {
                self._driver.SQLITE_BUSY, self._driver.SQLITE_LOCKED,
            }:
                code = "database_busy"
            else:
                code = "query_failed"
            raise DatabaseReadError(code) from None

    def execute(self, sql, parameters=()):
        deadline = time.monotonic() + self._query_timeout
        cursor = self._call(lambda: self._connection.execute(sql, parameters), deadline)
        return _ReadOnlyCursor(self, cursor, deadline)

    def close(self):
        if not self._closed:
            try:
                self._connection.close()
            except Exception:
                raise DatabaseReadError("query_failed") from None
            finally:
                self._closed = True


@contextmanager
def open_readonly(path, raw_key, *, query_timeout: float = DEFAULT_QUERY_TIMEOUT):
    """Open an existing active WAL database with a 32-byte encryption key.

The raw key is passed through sqlite3_key, never SQL text. The caller retains
ownership of raw_key and should clear mutable key buffers on shutdown. Missing
or uninitialized sidecars fail closed; inactive DELETE-mode shards are not
supported. No copy of any database is created, encrypted or decrypted.
The process must contain no writable SQLCipher connections to these files;
readonly_shm cannot revoke a pre-existing process-local writable SHM mapping.
    """
    if (isinstance(query_timeout, bool) or not isinstance(query_timeout, (int, float))
            or not math.isfinite(query_timeout) or not 0 < query_timeout <= 30):
        raise DatabaseReadError("configuration_invalid")
    source = _source_path(path)
    key = _key_literal(raw_key)
    connection = wrapped = None
    try:
        with _pin_sources(source):
            driver = _load_sqlcipher()
            try:
                try:
                    connection = driver.connect(
                        source.as_uri() + "?mode=ro&readonly_shm=1", uri=True,
                        flags=driver.SQLITE_OPEN_READONLY, isolation_level=None, cached_statements=0,
                        timeout=min(0.2, query_timeout),
                    )
                    connection.set_trace_callback(None)
                    connection.enable_load_extension(False)
                    # Even key/page errors must not go to native stderr/logs.
                    connection.execute("PRAGMA cipher_log_level=NONE")
                    connection.set_key(key)
                    key[:] = b"\x00" * len(key)
                    for sql in (
                        "PRAGMA cipher_compatibility=4", "PRAGMA temp_store=MEMORY",
                        "PRAGMA cipher_memory_security=ON", "PRAGMA query_only=ON",
                        f"PRAGMA cache_size=-{CACHE_KIB}", "PRAGMA mmap_size=0", "PRAGMA trusted_schema=OFF",
                    ):
                        connection.execute(sql)
                    version = connection.execute("PRAGMA cipher_version").fetchone()
                    if version != ("4.19.0 community",):
                        raise DatabaseReadError("dependency_version_unsupported")
                    connection.row_factory = driver.Row
                    from .db_text import sqlite_text

                    connection.text_factory = sqlite_text
                    wrapped = ReadOnlyConnection(connection, driver, query_timeout)
                    # Read only schema metadata to validate the key and WAL view.
                    wrapped.execute("SELECT count(*) FROM sqlite_master").fetchone()
                except DatabaseReadError:
                    raise
                except Exception:
                    raise DatabaseReadError("database_unreadable") from None
                yield wrapped
            finally:
                if wrapped is not None:
                    wrapped.close()
                elif connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        raise DatabaseReadError("database_unreadable") from None
    finally:
        key[:] = b"\x00" * len(key)
