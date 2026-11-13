"""Immutable, encrypted history for interpersonal-claim adjudication.

One decision is one row in the existing encrypted ``state`` table, keyed
``relation-adjudication:<claim>:<sequence>:<new version>``. That shape is
deliberate:

* the fixed-width sequence makes the claim prefix a range scan, so a page is one
  bounded query (``key > cursor ORDER BY key LIMIT n``) and never a full read of
  a growing array;
* the new claim version is part of the key, so a decision can never overwrite an
  earlier one: an occupied location is refused instead of merged;
* the value is written with ``Store.ledger_encode`` exactly like every other
  state row, so the existing portable and local backup/restore flows carry the
  history with the claim and no new artifact or format is introduced;
* nothing here deletes or rewrites an entry, so an earlier decision stays
  readable exactly as it was written.

Entries are written by the core ``RelationService.adjudicate`` inside the same
transaction as the claim status, the claim policy and (when the desktop entry
point is the caller) the desktop request receipt. The old web entry point keeps
writing the same history because it calls the same method.
"""
from __future__ import annotations

import re

from .errors import AssistantError

PREFIX = "relation-adjudication:"
FORMAT = 1
PAGE_MAX = 50
REASON_MAX = 1000
CLAIM = re.compile(r"[A-Za-z0-9_-]{1,80}")
CURSOR = re.compile(r"[0-9]{1,12}")


def claim_id(claim):
    if not isinstance(claim, str) or not CLAIM.fullmatch(claim):
        raise AssistantError("relation_claim", "关系声明标识格式无效，请重新读取。")
    return claim


def cursor(after):
    if not isinstance(after, str) or len(after) > 12 or (after and not CURSOR.fullmatch(after)):
        raise AssistantError("relation_history", "历史分页位置无效，请重新读取。")
    return after


def limit_of(limit):
    if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
        raise AssistantError("relation_history", f"每次只能查看 1 至 {PAGE_MAX} 条裁决历史。")
    return limit


def key(claim, sequence, version):
    return PREFIX + claim + ":" + ("%012d" % sequence) + ":" + version


def _pattern(claim):
    """Escape the LIKE wildcards: a claim id may contain ``_`` or ``-``."""
    escaped = claim.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return PREFIX + escaped + ":%"


def _start(claim, sequence):
    return PREFIX + claim + ":" + ("%012d" % sequence)


def boundary(claim, after):
    """Exclusive lower bound for the *next* sequence, or ``None`` past the end.

    The stored key carries ``:<new version>`` after the sequence, so comparing
    against ``prefix + after`` would include the entry just returned again.
    A fixed-width sequence makes ``prefix + (after + 1)`` an exact boundary that
    neither repeats nor skips an entry, and a short numeric cursor means the same
    thing as the padded one this module returns. A cursor beyond what a sequence
    can hold is past every possible entry and needs no boundary string at all.
    """
    cursor(after)
    sequence = int(after) if after else 0
    if sequence >= 10 ** 12 - 1:
        return None
    return _start(claim, sequence + 1)


def _sequence(location):
    parts = location.split(":")
    return parts[-2] if len(parts) > 1 else ""


def _fail(message):
    raise AssistantError("relation_history", message)


def _decode(store, location, value):
    """One stored entry, or an explicit marker that it cannot be read.

    A damaged payload is reported as unreadable instead of being skipped: an
    omitted decision would look like a decision that never happened.
    """
    try:
        entry = store.ledger_decode("state:" + location, value)
    except (AssistantError, ValueError):
        # A damaged payload is reported either way: the file store fails JSON
        # decoding, the encrypted database backend fails its integrity check.
        return {"cursor": _sequence(location), "unreadable": True, "error": "relation_history",
                "detail": "这一条裁决历史无法解读（完整性校验失败），本页不推测它的内容。"}
    if not isinstance(entry, dict) or type(entry.get("format")) is not int:
        return {"cursor": _sequence(location), "unreadable": True, "error": "relation_history",
                "detail": "这一条裁决历史的格式无法识别，本页不推测它的内容。"}
    return {**entry, "cursor": _sequence(location)}


def next_sequence(db, claim):
    row = db.execute("SELECT key FROM state WHERE key LIKE ? ESCAPE '\\' ORDER BY key DESC LIMIT 1",
                     (_pattern(claim),)).fetchone()
    if row is None:
        return 1
    try:
        return int(_sequence(row[0])) + 1
    except ValueError:
        _fail("关系裁决历史的索引格式无效，请先由维护流程核对；本次没有写入任何更改。")


def append(store, *, db, claim, before, after, actor, at, reason, snapshot, sources):
    """Append one immutable entry inside the caller's transaction.

    ``before``/``after`` carry the claim status, the claim version, the policy
    version and the policy validity; ``snapshot`` is the claim as it stood
    (subject/object/declarer/statement/evidence) and ``sources`` the evidence
    pins. Nothing is invented: a missing statement is recorded as missing.
    """
    claim_id(claim)
    if not isinstance(reason, str) or len(reason) > REASON_MAX:
        raise AssistantError("relation_reason", f"更正说明最多 {REASON_MAX} 字。")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise AssistantError("relation_history", "关系裁决历史缺少变更前后状态")
    sequence = next_sequence(db, claim)
    entry = {"format": FORMAT, "claim": claim, "sequence": sequence, "at": at, "actor": actor,
             "reason": reason, "before": dict(before), "after": dict(after),
             "restored": before.get("status") == "revoked" and after.get("status") != "revoked",
             "claim_snapshot": snapshot,
             "sources": [[source, version] for source, version in sources]}
    location = key(claim, sequence, after["version"])
    written = db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT DO NOTHING",
                         (location, store.ledger_encode("state:" + location, entry)))
    if written.rowcount != 1:
        _fail("这条裁决历史的写入位置已被占用，未写入任何更改；请重新读取后再确认。")
    return {"sequence": sequence, "at": at, "restored": entry["restored"], "cursor": str(sequence)}


def page(store, claim, after="", limit=20):
    """One bounded page ordered by the fixed-width sequence."""
    claim_id(claim)
    limit_of(limit)
    lower = boundary(claim, after)
    if lower is None:
        return {"items": [], "next": None, "count": 0, "limit": limit,
                "after": after, "page_max": PAGE_MAX}
    with store.db() as db:
        rows = db.execute(
            "SELECT key,value FROM state WHERE key LIKE ? ESCAPE '\\' AND key >= ? ORDER BY key LIMIT ?",
            (_pattern(claim), lower, limit + 1)).fetchall()
    items = [_decode(store, row["key"], row["value"]) for row in rows[:limit]]
    following = _sequence(rows[limit - 1]["key"]) if len(rows) > limit else None
    return {"items": items, "next": following, "count": len(items), "limit": limit,
            "after": after, "page_max": PAGE_MAX}


def bounds(store, claim):
    """First/last recorded decision and the exact count, as one aggregate."""
    claim_id(claim)
    with store.db() as db:
        summary = db.execute("SELECT COUNT(*) AS total, MIN(key) AS first_key, MAX(key) AS last_key "
                             "FROM state WHERE key LIKE ? ESCAPE '\\'", (_pattern(claim),)).fetchone()
        first = last = None
        if summary["first_key"]:
            row = db.execute("SELECT value FROM state WHERE key=?", (summary["first_key"],)).fetchone()
            first = None if row is None else _decode(store, summary["first_key"], row["value"])
        if summary["last_key"] and summary["last_key"] != summary["first_key"]:
            row = db.execute("SELECT value FROM state WHERE key=?", (summary["last_key"],)).fetchone()
            last = None if row is None else _decode(store, summary["last_key"], row["value"])
        else:
            last = first
    return {"count": summary["total"], "first": first, "last": last}
