"""Read-only, bounded evidence for a client-recorded text send.

There is no native/SQLite/UI implementation in this module. A trusted injected
reader supplies live metadata and bounded self echoes from one verified account
and one raw conversation ID. It must not read from the durable receive cursor.
No helper here sends, reads credentials, opens files, logs, or accesses a network.

Reader contract (private IPC adapters must validate the same shape):

* ``current_heads(scope)`` freshly reads the actual MAX(local_id) row, including
  server_id/create_time, of EVERY current shard for this exact target. It reads
  metadata only, returns an empty anchor only for an empty table, and fails if
  the head is pending. It does not return enrollment/acknowledged checkpoints.
* ``read_self_echo(scope, heads, limit=..., scan_limit=...)`` rereads each original
  anchor, reads current MAX heads, and covers ``old_max < local_id <= new_max``
  in committed per-shard snapshots. Only verified-self short text bodies may be
  returned. Pending sender/server IDs, undecodable text and budget exhaustion
  make ``complete`` false; they must not be skipped to manufacture completeness.
* For each returned server ID the reader checks metadata in ALL target shards
  at/below the original heads. ``server_id_is_new`` is true only if absent there;
  server IDs are NOT assumed to be monotone. ``scanned`` counts metadata rows
  examined in the new ranges and is bounded by scan_limit. The adapter must also
  bound SQL execution time and server-ID existence lookups.
* Scope identity, source catalog, file identity and per-shard generation/salt
  must still match. Schema/identity failures are errors, never empty results.

A watermark is local to the same reader object and is not serializable through
this API. A worker adapter should retain it in RAM under a single-use action
handle. Capture it immediately before UI dispatch; bind it to one fixed action,
prevent concurrent identical manual/automatic sends, and never reuse it after a
worker restart. ``client_recorded`` is evidence of a unique matching local client
record, not evidence that this action caused it, or that a recipient received or
read it. Echo timestamps must fall within this attempt's wall-clock window
(one second of timestamp granularity is allowed); local/server clock disagreement
fails closed. The caller remains responsible for the UI target/commit guard.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

_MAX_SOURCES = 16
_MAX_ROWS = 100
_MAX_SCAN = 512
_MAX_INT = 2**63 - 1
_LIVE = object()
_CODES = {
    "receipt_invalid_input", "receipt_invalid_report", "receipt_reader_failed",
    "receipt_reader_changed", "receipt_scope_changed", "receipt_catalog_changed",
    "receipt_generation_changed", "receipt_anchor_changed", "receipt_watermark_expired",
    "receipt_server_id_reused", "receipt_duplicate_record",
}


class ReceiptError(Exception):
    """Public error text contains only an allowlisted code."""

    def __init__(self, code="receipt_invalid_input"):
        self.code = code if isinstance(code, str) and code in _CODES else "receipt_invalid_input"
        super().__init__(self.code)


def _require(condition, code="receipt_invalid_report"):
    if not condition:
        raise ReceiptError(code)


def _identifier(value):
    return (type(value) is str and 0 < len(value) <= 256 and bool(value.strip())
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))


def _integer(value, low=1):
    return type(value) is int and low <= value <= _MAX_INT


def _server_id(value):
    return (type(value) is str and re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None
            and int(value) <= _MAX_INT)


def _text(value, *, nonempty=False):
    if type(value) is not str or len(value) > 4096 or (nonempty and not value) or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8", "strict")) <= 4096
    except UnicodeError:
        return False


def _keys(value, fields):
    _require(type(value) is dict and set(value) == set(fields))


@dataclass(frozen=True, repr=False)
class ReceiptScope:
    account_id: str
    target_ref: str
    self_ref: str
    source_generation: str

    def __post_init__(self):
        _require(all(_identifier(v) for v in (self.account_id, self.target_ref,
                                             self.self_ref, self.source_generation))
                 and self.target_ref != self.self_ref and not self.self_ref.endswith("@chatroom"),
                 "receipt_invalid_input")

    def private_dict(self):
        return {"account_id": self.account_id, "target_ref": self.target_ref,
                "self_ref": self.self_ref, "source_generation": self.source_generation}


@dataclass(frozen=True, repr=False)
class ReceiptAnchor:
    local_id: int
    server_id: str
    create_time: int

    def private_dict(self):
        return {"local_id": self.local_id, "server_id": self.server_id, "create_time": self.create_time}


@dataclass(frozen=True, repr=False)
class ReceiptHead:
    source_id: str
    generation: str
    anchor: ReceiptAnchor | None

    def private_dict(self):
        return {"source_id": self.source_id, "generation": self.generation,
                "anchor": None if self.anchor is None else self.anchor.private_dict()}


@dataclass(frozen=True, repr=False)
class SendWatermark:
    scope: ReceiptScope
    heads: tuple[ReceiptHead, ...]
    captured_at: float
    not_before: int
    _reader: object = field(compare=False)
    _origin: object = field(compare=False)


@dataclass(frozen=True, repr=False)
class ReceiptRecord:
    """Private identity of the matched local record; deliberately no text."""

    source_id: str
    local_id: int
    server_id: str
    create_time: int


@dataclass(frozen=True)
class ReceiptResult:
    status: str
    reason: str
    record: ReceiptRecord | None = field(default=None, repr=False)

    @property
    def confirmed(self):
        return self.status == "client_recorded"


class ReceiptReader(Protocol):
    def current_heads(self, scope: ReceiptScope) -> dict: ...

    def read_self_echo(self, scope: ReceiptScope, heads: tuple[ReceiptHead, ...], *,
                       limit: int, scan_limit: int) -> dict: ...


def _anchor(value):
    if value is None:
        return None
    _keys(value, {"local_id", "server_id", "create_time"})
    _require(_integer(value["local_id"]) and _server_id(value["server_id"])
             and _integer(value["create_time"]))
    return ReceiptAnchor(value["local_id"], value["server_id"], value["create_time"])


def _heads(value):
    _require(type(value) is list and 1 <= len(value) <= _MAX_SOURCES)
    result = []
    for item in value:
        _keys(item, {"source_id", "generation", "anchor"})
        _require(_identifier(item["source_id"]) and _identifier(item["generation"]))
        result.append(ReceiptHead(item["source_id"], item["generation"], _anchor(item["anchor"])))
    _require(len({h.source_id for h in result}) == len(result))
    return tuple(sorted(result, key=lambda h: h.source_id))


def _report(value, scope, fields):
    _keys(value, fields | {"schema_version", "scope", "heads"})
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    _keys(value["scope"], {"account_id", "target_ref", "self_ref", "source_generation"})
    _require(value["scope"] == scope.private_dict(), "receipt_scope_changed")
    return _heads(value["heads"])


def _now(clock):
    value = clock()
    _require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
             "receipt_invalid_input")
    return value


def begin_watermark(reader: ReceiptReader, scope: ReceiptScope, *,
                    clock=time.monotonic, wall_clock=time.time) -> SendWatermark:
    """Capture fresh metadata heads; a failure must prevent subsequent dispatch.

    A receive checkpoint or SourceSession.snapshot() is intentionally not an
    accepted input. The reader, not the caller, must perform fresh head queries.
    """
    _require(type(scope) is ReceiptScope, "receipt_invalid_input")
    started = _now(clock)
    not_before = int(_now(wall_clock))
    _require(_integer(not_before), "receipt_invalid_input")
    try:
        report = reader.current_heads(scope)
    except Exception:
        raise ReceiptError("receipt_reader_failed") from None
    heads = _report(report, scope, set())
    finished = _now(clock)
    _require(started <= finished and finished - started <= 20, "receipt_watermark_expired")
    # The earliest shard query is the conservative start of the validity window.
    return SendWatermark(scope, heads, started, not_before, reader, _LIVE)


def _check_heads(watermark, current, anchors):
    old = {h.source_id: h for h in watermark.heads}
    new = {h.source_id: h for h in current}
    _require(set(old) == set(new), "receipt_catalog_changed")
    _require(type(anchors) is dict and set(anchors) == set(old), "receipt_anchor_changed")
    for key, head in old.items():
        latest = new[key]
        _require(head.generation == latest.generation, "receipt_generation_changed")
        _require(_anchor(anchors[key]) == head.anchor, "receipt_anchor_changed")
        if head.anchor is not None:
            _require(latest.anchor is not None and latest.anchor.local_id >= head.anchor.local_id,
                     "receipt_anchor_changed")
            if latest.anchor.local_id == head.anchor.local_id:
                _require(latest.anchor == head.anchor, "receipt_anchor_changed")
    return old, new


def _matches(report, watermark, text, limit, scan_limit, not_after):
    current = _report(report, watermark.scope, {"anchors", "rows", "complete", "scanned"})
    old, new = _check_heads(watermark, current, report["anchors"])
    rows = report["rows"]
    _require(type(rows) is list and len(rows) <= limit and type(report["complete"]) is bool
             and _integer(report["scanned"], 0) and len(rows) <= report["scanned"] <= scan_limit)
    matches, local_ids, server_ids = [], set(), set()
    old_server_ids = {h.anchor.server_id for h in old.values() if h.anchor is not None}
    scope = watermark.scope
    for row in rows:
        _keys(row, {"source_id", "generation", "account_id", "target_ref", "sender_ref",
                    "local_id", "server_id", "create_time", "text", "server_id_is_new"})
        _require(_identifier(row["source_id"]) and row["source_id"] in old)
        before, after = old[row["source_id"]], new[row["source_id"]]
        _require(row["generation"] == before.generation, "receipt_generation_changed")
        _require(row["account_id"] == scope.account_id and row["target_ref"] == scope.target_ref
                 and row["sender_ref"] == scope.self_ref, "receipt_scope_changed")
        anchor = _anchor({k: row[k] for k in ("local_id", "server_id", "create_time")})
        lower = before.anchor.local_id if before.anchor is not None else 0
        _require(after.anchor is not None and lower < anchor.local_id <= after.anchor.local_id)
        if anchor.local_id == after.anchor.local_id:
            _require(anchor == after.anchor, "receipt_anchor_changed")
        _require(type(row["server_id_is_new"]) is bool and row["server_id_is_new"]
                 and anchor.server_id not in old_server_ids, "receipt_server_id_reused")
        local_key = (before.source_id, anchor.local_id)
        _require(local_key not in local_ids and anchor.server_id not in server_ids,
                 "receipt_duplicate_record")
        local_ids.add(local_key)
        server_ids.add(anchor.server_id)
        _require(_text(row["text"]))
        # A history import can have a new local ID and an unseen server ID.
        # The client's second-resolution timestamp must also be from this
        # attempt's time window. Clock disagreement fails closed; it is not an
        # excuse to confirm an old message. This does not prove causality.
        if row["text"] == text and watermark.not_before <= anchor.create_time <= not_after:
            matches.append(ReceiptRecord(before.source_id, anchor.local_id,
                                         anchor.server_id, anchor.create_time))
    return matches


def verify_receipt(reader: ReceiptReader, watermark: SendWatermark, text: str, *,
                   limit=100, scan_limit=512, max_age=120, clock=time.monotonic) -> ReceiptResult:
    """Read once and verify a unique new self echo; never retry a UI action.

    Incomplete reads cannot confirm even when one candidate is already visible:
    another identical echo may be outside the bounded response. Callers may poll
    again with the SAME watermark while within max_age. Errors are private-code
    results; no provider exception, message body or identity enters diagnostics.
    """
    _require(type(watermark) is SendWatermark and watermark._origin is _LIVE
             and _text(text, nonempty=True), "receipt_invalid_input")
    _require(type(limit) is int and 1 <= limit <= _MAX_ROWS
             and type(scan_limit) is int and limit <= scan_limit <= _MAX_SCAN
             and type(max_age) in (int, float) and math.isfinite(max_age) and 0 < max_age <= 300,
             "receipt_invalid_input")
    if reader is not watermark._reader:
        return ReceiptResult("uncertain", "receipt_reader_changed")
    try:
        age = _now(clock) - watermark.captured_at
        _require(0 <= age <= max_age, "receipt_watermark_expired")
        try:
            report = reader.read_self_echo(watermark.scope, watermark.heads,
                                           limit=limit, scan_limit=scan_limit)
        except Exception:
            raise ReceiptError("receipt_reader_failed") from None
        age = _now(clock) - watermark.captured_at
        _require(0 <= age <= max_age, "receipt_watermark_expired")
        matches = _matches(report, watermark, text, limit, scan_limit,
                           watermark.not_before + math.ceil(age) + 1)
    except ReceiptError as exc:
        return ReceiptResult("uncertain", exc.code)
    if len(matches) > 1:
        return ReceiptResult("ambiguous", "multiple_matching_echoes")
    if not report["complete"]:
        return ReceiptResult("pending", "incomplete_scan")
    if not matches:
        return ReceiptResult("pending", "no_matching_echo")
    return ReceiptResult("client_recorded", "unique_self_echo", matches[0])
