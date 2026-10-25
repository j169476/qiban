"""Routing-only projection. Every candidate still needs current source/ACL checks."""
from datetime import datetime

KINDS = ("conversation_turns", "conversation_summaries")


def write(db, kind, data):
    if kind not in KINDS:
        return
    stamp = data.get("occurred_at") or data["source"]["recorded_at"]
    db.execute("""INSERT INTO conversation_index VALUES(?,?,?,?,?,?,?) ON CONFLICT(kind,id)
        DO UPDATE SET account=excluded.account,thread_id=excluded.thread_id,stamp=excluded.stamp,
        sequence=excluded.sequence,part=excluded.part""", (kind, data["id"], data["account_uid"],
        data["thread_id"], datetime.fromisoformat(stamp).timestamp(), data.get("sequence", 0), data.get("part", 0)))


def backfill(store):
    with store.lock, store.db() as db:
        rows = db.execute("SELECT c.* FROM content_records c LEFT JOIN conversation_index i USING(kind,id) "
                          "WHERE c.kind=ANY(?) AND i.id IS NULL", (list(KINDS),)).fetchall()
        for row in rows:
            write(db, row["kind"], store._decode(row["kind"], row["id"], row["version"], row["body"])["data"])


def candidates(store, kind, account, tid):
    with store.db() as db:
        return [row[0] for row in db.execute("SELECT id FROM conversation_index WHERE kind=? AND account=? AND thread_id=?",
                                             (kind, account, tid))]
