"""Authenticated keyset pages with bounded plaintext search and revocation checks."""
import base64
import json
import math

from . import conversations
from .access import AccessService
from .errors import AssistantError
from .governance import GovernanceRepository, verify_record_versions


def cursor(row):
    return base64.urlsafe_b64encode(json.dumps([row["stamp"], row["id"]]).encode()).decode()


def parse(value):
    try:
        if not isinstance(value, str) or len(value) > 400:
            raise ValueError()
        stamp, key = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if type(stamp) not in (float, int) or not math.isfinite(stamp) or not isinstance(key, str) or len(key) > 100:
            raise ValueError()
        return stamp, key
    except (ValueError, TypeError):
        raise AssistantError("invalid_cursor", "历史位置无效，请返回最新消息") from None


def page(store, authority, request, account, channel, *, before="", after="", query="", limit=40, known=(), known_versions=None):
    authority.require(request)
    known_versions = {} if known_versions is None else known_versions
    if (before and after or not isinstance(query, str) or len(query) > 200
            or type(limit) is not int or not 1 <= limit <= 100 or len(known) > 100
            or any(not isinstance(key, str) or len(key) > 100 for key in known)
            or not isinstance(known_versions, dict) or len(known_versions) > 100
            or any(key not in known or not isinstance(value, str) or len(value) > 80 for key, value in known_versions.items())):
        raise AssistantError("invalid_history", "历史查询参数无效")
    repository = GovernanceRepository(store)
    access = AccessService(repository, authority)
    tid = conversations.thread_id(account, channel, request.conversation)
    args = [account, tid]
    boundary = ""
    if before or after:
        boundary = " AND (stamp,id) " + (">" if after else "<") + " (?,?)"
        args.extend(parse(before or after))
    size = 200 if query else limit
    args.append(size + 1)
    with store.db() as db:
        rows = db.execute("SELECT id,stamp FROM conversation_index WHERE kind='conversation_turns' "
                          "AND account=? AND thread_id=?" + boundary + " ORDER BY stamp "
                          + ("ASC,id ASC" if after else "DESC,id DESC") + " LIMIT ?", args).fetchall()
        known_rows = db.execute("SELECT i.id,i.stamp,c.version FROM conversation_index i JOIN content_records c USING(kind,id) "
            "WHERE i.kind='conversation_turns' AND i.account=? AND i.thread_id=? AND i.id=ANY(?)",
            (account, tid, list(known))).fetchall()
    scoped_known = {row["id"] for row in known_rows}
    more, rows = len(rows) > size, rows[:size]
    roots = ["conversation_turns/" + row["id"] for row in rows]
    roots.extend("conversation_turns/" + key for key in scoped_known)
    allowed = repository.readable_resources(access, request, candidates=roots)
    selected = [row for row in rows if "conversation_turns/" + row["id"] in allowed]
    changes = [row for row in known_rows if row["id"] in known_versions and known_versions[row["id"]] != row["version"]
               and "conversation_turns/" + row["id"] in allowed]
    records, issues = store.records_selected("conversation_turns", {row["id"] for row in [*selected, *changes]})
    if issues:
        raise AssistantError("storage_integrity", "历史内容校验失败")
    documents = {item["data"]["id"]: item for item in records}
    ordered, updates = [], []
    for row in [*selected, *changes]:
        item = documents.get(row["id"])
        if not item:
            continue
        data = item["data"]
        if data["account_uid"] != account or data["thread_id"] != tid or data["thread_key"] != request.conversation:
            raise AssistantError("storage_integrity", "历史索引范围不一致")
        value = {"id": data["id"], "speaker": data["speaker"], "text": data["content"], "version": item["version"],
                 "at": data["occurred_at"], "attachments": data.get("attachments", []), "cursor": cursor(row)}
        if row in changes:
            updates.append(value)
        if row in selected and query.casefold() in data["content"].casefold():
            ordered.append(value)
    # Search may inspect 200 candidates, but never discloses more than 100 roots in one page.
    ordered = ordered[:limit]
    if not after:
        ordered.reverse()
    included = {item["id"] for item in [*ordered, *updates]}
    # Re-check known visible entries too: an unchanged page must reflect revocations.
    valid_known = [key for key in known if key in scoped_known and "conversation_turns/" + key in allowed]
    lease = access.lease(request, ["conversation_turns/" + key for key in sorted(included | set(valid_known))])
    verify_record_versions(store, {("conversation_turns", key): documents[key]["version"] for key in included})
    access.revalidate(lease)
    # Search stops at the last disclosed result when its page is full, so it cannot skip matches.
    edge = next((row for row in rows if row["id"] == ordered[0 if not after else -1]["id"]), None) if ordered else None
    scanned = edge if len(ordered) == limit else (rows[-1] if rows else None)
    return {"messages": ordered, "updates": updates, "next": cursor(scanned) if scanned and (more or len(ordered) == limit) else "",
            "newest": cursor(rows[-1] if after else rows[0]) if rows else after,
            "removed": [key for key in known if key not in valid_known], "scanned": len(rows),
            "search_limited": bool(query and more)}
