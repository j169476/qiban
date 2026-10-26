"""Private read-only receipt adapter over an already authenticated SourceSession.

No connection/key discovery, SQL writes, UI actions, network or plaintext files.
Only the dedicated database worker may instantiate this adapter. Handles and
watermarks stay in RAM and are invalid after worker exit. The receiving durable
checkpoint is used solely for source identity/salt, NEVER as a send watermark.
"""
from __future__ import annotations

import hashlib
import math
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import send_receipt as proof
from .db_probe import message_table
from .db_source import SourceError, _integer, _list_shards, _username, validate_enrollment
from .db_text import short_text

_CODES = {"receipt_invalid_input", "receipt_action_busy", "receipt_capacity_exceeded",
          "receipt_closed", "receipt_source_pending"}
_HANDLE = re.compile(r"[0-9a-f]{64}")
_TTL = 120


class DatabaseReceiptError(Exception):
    def __init__(self, code="receipt_invalid_input"):
        self.code = code if code in _CODES else "receipt_invalid_input"
        super().__init__(self.code)


class DatabaseReceiptReader:
    """Read only this fixed raw target using the session's existing connections.

    ``client_fn`` is an injectable metadata-only identity observer. Production
    compares the authenticated session's pinned PID, executable and version.
    Another logged-in client cannot redirect or invalidate this selection.
    Tests supply a synthetic observer, never Windows.
    New-range scans return <=512 metadata rows and <=100 self bodies. Historical
    server-ID membership adds at most one bounded metadata query per shard, with
    <=100 returned IDs per query; the SQL facade has a per-query timeout and this
    adapter additionally checks an 8-second total-operation deadline.
    """

    def __init__(self, session, target_ref, *, client_fn=None, clock=time.monotonic):
        session._require_open()
        self._session = session
        self._enrollment = validate_enrollment(session._enrollment)
        matches = [key for key, target in self._enrollment["targets"].items()
                   if target["username"] == target_ref]
        if type(target_ref) is not str or len(matches) != 1:
            raise DatabaseReceiptError()
        self._alias = matches[0]
        self.scope = proof.ReceiptScope(self._enrollment["account_id"], target_ref,
                                       self._enrollment["self_username"], self._enrollment["epoch"])
        if client_fn is None:
            from .db_windows import discover_wechat

            client_pid = session._prepared.client.pid

            def client_fn():
                return discover_wechat(pid=client_pid)
        self._client_fn, self._clock = client_fn, clock
        self._client = session._prepared.client
        self._client_signature = (self._client.pid, self._client.executable, self._client.version)
        self._all_sources = dict(session._sources)
        self._source_tokens = {key: (source.connection, source.path, source.inode)
                               for key, source in self._all_sources.items()}
        self._items = {key: {k: item[k] for k in ("shard", "target_ref", "salt")}
                       for key, item in session._checkpoint["sources"].items()}
        self._sources = {key: source for key, source in self._all_sources.items()
                         if source.target.alias == self._alias}
        self._catalog = tuple(session._prepared.catalog)
        self._deadline = 0
        self.pending = False
        if not 1 <= len(self._sources) <= 16 or set(self._items) != set(self._all_sources):
            raise SourceError("source_catalog_changed")
        # A prepared shard with neither authorized table is not exposed by
        # SourceSession. Do not silently ignore a new table appearing there.
        if {source.path.name for source in self._all_sources.values()} != set(self._catalog):
            raise SourceError("source_catalog_changed")

    def __repr__(self):
        return "<DatabaseReceiptReader private read-only>"

    def _start(self):
        self.pending = False
        self._deadline = self._clock() + 8.0

    def _budget(self):
        if self._clock() >= self._deadline:
            raise SourceError("source_read_failed")

    def _query(self, connection, sql, parameters=()):
        self._budget()
        result = connection.execute(sql, parameters).fetchall()
        self._budget()
        return result

    def _guard(self, scope):
        self._budget()
        session = self._session
        session._require_open()
        observed = self._client_fn()
        if (scope != self.scope or validate_enrollment(session._enrollment) != self._enrollment
                or session._prepared.client != self._client
                or session._prepared.self_username != self.scope.self_ref
                or (self._client.pid, self._client.executable, self._client.version) != self._client_signature
                or (observed.pid, observed.executable, observed.version) != self._client_signature):
            raise SourceError("source_identity_changed")
        if session._state(self._client.pid).get("window_count", 0) == 0:
            raise SourceError("client_not_running")
        if (tuple(path.name for path in _list_shards(session._prepared.account)) != self._catalog
                or tuple(session._prepared.catalog) != self._catalog
                or set(session._sources) != set(self._all_sources)):
            raise SourceError("source_catalog_changed")
        tables = {}
        for key, source in self._all_sources.items():
            item = self._items[key]
            expected = self._enrollment["targets"][item["target_ref"]]["username"]
            current = session._checkpoint["sources"].get(key)
            connection, path, inode = self._source_tokens[key]
            if (session._sources[key] is not source or source.target.username != expected
                    or source.target.alias != item["target_ref"] or source.table != message_table(expected)
                    or source.connection is not connection or source.path != path or source.inode != inode
                    or source.path.relative_to(session._prepared.account / "db_storage").as_posix()
                    != item["shard"] or current is None
                    or any(current[k] != item[k] for k in item)):
                raise SourceError("source_identity_changed")
            session._check_generation(source, item)
            tables.setdefault(source.path.name, (source.connection, set()))[1].add(source.table)
        allowed = tuple(message_table(target["username"])
                        for target in self._enrollment["targets"].values())
        for connection, expected in tables.values():
            present = self._query(connection,
                                  "SELECT name FROM sqlite_master WHERE type='table' AND name IN ("
                                  + ",".join("?" for _ in allowed) + ")",
                                  allowed)
            if {row[0] for row in present} != expected:
                raise SourceError("source_catalog_changed")
        self._budget()

    @contextmanager
    def _transactions(self):
        active = []
        try:
            for source in self._sources.values():
                self._budget()
                connection = source.connection
                if any(connection is existing for existing in active):
                    raise SourceError("source_identity_changed")
                connection.execute("BEGIN")
                active.append(connection)
            yield
        finally:
            failure = None
            for connection in reversed(active):
                try:
                    connection.execute("ROLLBACK")
                except Exception as exc:
                    failure = exc
            if failure is not None:
                raise SourceError("source_read_failed") from None

    def _anchor(self, row):
        if row is None:
            return None
        if len(row) != 3 or not _integer(row[0], 1):
            raise SourceError("source_schema_unsupported")
        if not _integer(row[1], 1) or not _integer(row[2], 1):
            self.pending = True
            raise DatabaseReceiptError("receipt_source_pending")
        return {"local_id": row[0], "server_id": str(row[1]), "create_time": row[2]}

    def _latest(self, source):
        rows = self._query(source.connection,
                           f'SELECT local_id,server_id,create_time FROM "{source.table}" '
                           'ORDER BY local_id DESC LIMIT 2')
        if len(rows) == 2 and rows[0][0] == rows[1][0]:
            raise SourceError("source_schema_unsupported")
        return self._anchor(rows[0] if rows else None)

    def _head(self, key, source):
        return {"source_id": key, "generation": self._items[key]["salt"], "anchor": self._latest(source)}

    def current_heads(self, scope):
        self._start()
        self._guard(scope)
        with self._transactions():
            heads = [self._head(key, source) for key, source in self._sources.items()]
        self._guard(scope)
        return {"schema_version": 1, "scope": scope.private_dict(), "heads": heads}

    def read_self_echo(self, scope, heads, *, limit, scan_limit):
        self._start()
        if (type(limit) is not int or not 1 <= limit <= 100 or type(scan_limit) is not int
                or not limit <= scan_limit <= 512 or type(heads) is not tuple
                or not all(type(head) is proof.ReceiptHead for head in heads)):
            raise DatabaseReceiptError()
        old = {head.source_id: head for head in heads}
        if len(old) != len(heads) or set(old) != set(self._sources):
            raise SourceError("source_catalog_changed")
        self._guard(scope)
        latest, anchors, rows, complete, scanned, body_reads = [], {}, [], True, 0, 0
        with self._transactions():
            for key, source in self._sources.items():
                if old[key].generation != self._items[key]["salt"]:
                    raise SourceError("source_generation_changed")
                head = self._head(key, source)
                latest.append(head)
                anchor = old[key].anchor
                if anchor is None:
                    anchors[key] = None
                else:
                    found = self._query(source.connection,
                                        f'SELECT local_id,server_id,create_time FROM "{source.table}" '
                                        'WHERE local_id=? LIMIT 2', (anchor.local_id,))
                    if len(found) != 1 or self._anchor(found[0]) != anchor.private_dict():
                        raise SourceError("source_anchor_changed")
                    anchors[key] = anchor.private_dict()
                if (anchor is not None and (head["anchor"] is None
                        or head["anchor"]["local_id"] < anchor.local_id)):
                    raise SourceError("source_anchor_changed")
            current = {head["source_id"]: head for head in latest}
            count = len(self._sources)
            for index, (key, source) in enumerate(self._sources.items()):
                quota = scan_limit // count + (index < scan_limit % count)
                lower = old[key].anchor.local_id if old[key].anchor else 0
                upper = current[key]["anchor"]["local_id"] if current[key]["anchor"] else 0
                if upper <= lower:
                    continue
                if quota == 0:
                    complete = False
                    continue
                batch = self._query(
                    source.connection,
                    f'SELECT m.local_id,m.server_id,m.create_time,m.local_type,n.user_name '
                    f'FROM "{source.table}" m LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id '
                    'WHERE m.local_id>? AND m.local_id<=? ORDER BY m.local_id LIMIT ?',
                    (lower, upper, quota),
                )
                scanned += len(batch)
                if not batch or batch[-1][0] != upper:
                    complete = False
                last = lower
                for raw in batch:
                    local_id, server_id, created, kind, sender = raw
                    if not _integer(local_id, 1) or not last < local_id <= upper:
                        raise SourceError("source_schema_unsupported")
                    last = local_id
                    if (not _integer(server_id, 1) or not _integer(created, 1)
                            or not _integer(kind, 1) or not _username(sender)):
                        complete = False
                        continue
                    if self._enrollment["targets"][self._alias]["kind"] == "private" and sender not in (scope.self_ref, scope.target_ref):
                        complete = False
                        continue
                    if sender != scope.self_ref or kind != 1:
                        continue
                    if body_reads >= limit:
                        complete = False
                        continue
                    body_reads += 1
                    body = self._query(
                        source.connection,
                        'SELECT CASE WHEN length(CAST(message_content AS BLOB))<=4096 '
                        f'THEN message_content ELSE NULL END FROM "{source.table}" '
                        'WHERE local_id=? AND server_id=? AND local_type=1 LIMIT 1',
                        (local_id, server_id),
                    )
                    if len(body) != 1:
                        raise SourceError("source_read_failed")
                    content = body[0][0]
                    text = short_text(content)
                    if not proof._text(text):
                        complete = False
                        continue
                    if self._enrollment["targets"][self._alias]["kind"] == "group" and text.startswith(sender + ":\n"):
                        text = text[len(sender) + 2:]
                    rows.append({"source_id": key, "generation": self._items[key]["salt"],
                                 "account_id": scope.account_id, "target_ref": scope.target_ref,
                                 "sender_ref": sender, "local_id": local_id, "server_id": str(server_id),
                                 "create_time": created, "text": text, "server_id_is_new": True})
            # No body reads here: an unseen/non-monotone server ID must not be
            # confused with an old server ID reinserted at a new local ID.
            candidates = sorted({int(row["server_id"]) for row in rows})
            historical = set()
            if candidates:
                placeholders = ",".join("?" for _ in candidates)
                for key, source in self._sources.items():
                    lower = old[key].anchor.local_id if old[key].anchor else 0
                    if lower:
                        found = self._query(source.connection,
                                            f'SELECT DISTINCT server_id FROM "{source.table}" '
                                            f'WHERE local_id<=? AND server_id IN ({placeholders}) LIMIT ?',
                                            (lower, *candidates, len(candidates)))
                        historical.update(str(row[0]) for row in found)
            for row in rows:
                row["server_id_is_new"] = row["server_id"] not in historical
        self._guard(scope)
        return {"schema_version": 1, "scope": scope.private_dict(), "heads": latest,
                "anchors": anchors, "rows": rows, "complete": complete, "scanned": scanned}


@dataclass(repr=False)
class _Entry:
    action_id: str
    target_ref: str
    reader: DatabaseReceiptReader
    watermark: proof.SendWatermark
    text_digest: bytes | None = field(default=None, repr=False)


class ReceiptHandles:
    """At most eight RAM handles, additionally one active handle per target."""

    def __init__(self, session, *, client_fn=None, clock=time.monotonic, wall_clock=time.time):
        self._session, self._client_fn = session, client_fn
        self._clock, self._wall_clock = clock, wall_clock
        self._entries = {}
        self._closed = False

    def __repr__(self):
        return "<ReceiptHandles private read-only>"

    def _prune(self):
        if self._closed:
            raise DatabaseReceiptError("receipt_closed")
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise DatabaseReceiptError()
        for handle, entry in tuple(self._entries.items()):
            if not 0 <= now - entry.watermark.captured_at <= _TTL:
                self._entries.pop(handle)

    @staticmethod
    def _action(action_id):
        if not proof._identifier(action_id):
            raise DatabaseReceiptError()

    def begin(self, action_id, target_ref):
        self._prune()
        self._action(action_id)
        if any(entry.action_id == action_id or entry.target_ref == target_ref
               for entry in self._entries.values()):
            raise DatabaseReceiptError("receipt_action_busy")
        if len(self._entries) >= 8:
            raise DatabaseReceiptError("receipt_capacity_exceeded")
        reader = DatabaseReceiptReader(self._session, target_ref, client_fn=self._client_fn, clock=self._clock)
        watermark = proof.begin_watermark(reader, reader.scope, clock=self._clock, wall_clock=self._wall_clock)
        handle = secrets.token_hex(32)
        if handle in self._entries:
            raise DatabaseReceiptError("receipt_capacity_exceeded")
        self._entries[handle] = _Entry(action_id, target_ref, reader, watermark)
        return {"handle": handle}

    def check(self, action_id, handle, text):
        self._prune()
        self._action(action_id)
        if type(handle) is not str or not _HANDLE.fullmatch(handle) or not proof._text(text, nonempty=True):
            raise DatabaseReceiptError()
        entry = self._entries.get(handle)
        if entry is None:
            return {"status": "uncertain", "confirmed": False}
        if entry.action_id != action_id:
            raise DatabaseReceiptError()
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        if entry.text_digest is not None and entry.text_digest != digest:
            self._entries.pop(handle)
            return {"status": "uncertain", "confirmed": False}
        entry.text_digest = digest
        result = proof.verify_receipt(entry.reader, entry.watermark, text, clock=self._clock)
        if (result.status == "uncertain" and result.reason == "receipt_reader_failed"
                and entry.reader.pending):
            return {"status": "pending", "confirmed": False}
        if result.status != "pending":
            self._entries.pop(handle)
        return {"status": result.status, "confirmed": result.confirmed}

    def discard(self, action_id, handle):
        self._prune()
        self._action(action_id)
        if type(handle) is not str or not _HANDLE.fullmatch(handle):
            raise DatabaseReceiptError()
        entry = self._entries.get(handle)
        if entry is not None and entry.action_id != action_id:
            raise DatabaseReceiptError()
        self._entries.pop(handle, None)
        return {"discarded": True}

    def close(self):
        self._entries.clear()
        self._closed = True
