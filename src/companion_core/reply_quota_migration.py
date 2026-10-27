"""Recover legacy shared daily usage from exact encrypted send/event receipts."""
from dataclasses import asdict

from .contracts import stable_key
from .errors import AssistantError
from .reply_authorization import ReplyScope

EMPTY = {"confirmed": 0, "reserved": 0, "uncertain": 0}
FIELDS = {"succeeded": "confirmed", "reserved": "reserved", "uncertain": "uncertain"}


def migrate(store, scope, day, db=None):
    legacy = stable_key("private-reply-usage", scope.platform, scope.account_identity, day)
    marker = "reply-quota-migrated:" + legacy
    if store.state(marker, False, db):
        return
    if db is None:
        with store.lock, store.db() as connection:
            return migrate(store, scope, day, connection)
    db.execute("BEGIN IMMEDIATE")
    if store.state(marker, False, db):
        return
    old = store.state(legacy, EMPTY, db)
    totals, buckets, records = dict(EMPTY), {}, []
    if any(old.values()):
        rows = db.execute("SELECT s.key,s.value,a.body FROM state s LEFT JOIN core_actions a "
                          "ON s.key='reply-send:' || a.id WHERE s.key LIKE 'reply-send:%'").fetchall()
        for row in rows:
            record = store.ledger_decode("state:" + row["key"], row["value"])
            if record.get("bucket") != legacy or record.get("window") is not None:
                continue
            field = FIELDS.get(record["status"])
            if field is None:
                continue
            try:
                event = store.ledger_decode("core_actions.body", row["body"])["event"]
                if event["platform"] != scope.platform or event["visibility"] != "private":
                    raise ValueError()
                meta = event["metadata"][scope.platform]
                target = meta["target_id" if scope.platform == "qq" else "target_ref"]
                # The exact old bucket identifies the verified account and day;
                # the immutable action event identifies the recipient.
                recipient = ReplyScope(scope.platform, scope.account_identity, "private", target)
            except (KeyError, TypeError, ValueError):
                raise AssistantError("reply_quota_migration_required", "旧额度缺少可核对的发送归属，请在管理页复核") from None
            key = stable_key("private-reply-usage-v2", recipient.id, day)
            buckets.setdefault(key, dict(EMPTY))[field] += 1
            totals[field] += 1
            records.append((row["key"], record | {"bucket": key, "scope": asdict(recipient)}))
        if totals != {k: old.get(k, 0) for k in EMPTY}:
            raise AssistantError("reply_quota_migration_required", "旧额度与发送账本不一致，请复核后迁移；已用量未清零")
    for key, counts in buckets.items():
        current = store.state(key, EMPTY, db)
        store.set_state(key, {field: current[field] + counts[field] for field in EMPTY}, db)
    for key, record in records:
        store.set_state(key, record, db)
    store.set_state(marker, {"version": 2, "recovered": totals}, db)
