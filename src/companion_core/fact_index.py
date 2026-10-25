"""Scope-only fact lookup; authorization always precedes projection and body reads."""
import hashlib
import hmac
import json
from datetime import datetime


def key(store, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    # Low-entropy values (cities, names) must not be dictionary-searchable in SQL.
    secret = getattr(store, "_backup_key", b"file-store-in-memory-projection")
    return hmac.new(secret, b"personal-fact-index-v1:" + raw, hashlib.sha256).hexdigest()


def scope(store, actor, account, conversation):
    return key(store, [actor, account, conversation])


def describe(store, record):
    data = record["data"]
    fact = data["fact"]
    return {"id": data["id"], "version": record["version"], "attribute": fact["attribute"],
            "value_key": key(store, fact["value"].casefold()), "valid_from": fact["valid_from"],
            "supersedes": fact["supersedes"]}


def write(store, db, kind, data, version):
    if kind != "memories":
        return
    if not data.get("fact") or not data["id"].startswith("fact-"):
        db.execute("DELETE FROM personal_fact_index WHERE kind=? AND id=?", (kind, data["id"]))
        return
    fact = data["fact"]
    db.execute("""INSERT INTO personal_fact_index VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(kind,id)
        DO UPDATE SET scope=excluded.scope,attribute=excluded.attribute,value_key=excluded.value_key,
        valid_from=excluded.valid_from,supersedes=excluded.supersedes,version=excluded.version,
        stamp=excluded.stamp""", (kind, data["id"], scope(store, fact["actor"], fact["account"], fact["conversation"]),
        fact["attribute"], key(store, fact["value"].casefold()), fact["valid_from"],
        json.dumps(fact["supersedes"]), version, datetime.fromisoformat(fact["valid_from"]).timestamp()))


def backfill(store):
    with store.lock, store.db() as db:
        rows = db.execute("SELECT c.* FROM content_records c LEFT JOIN personal_fact_index i USING(kind,id) "
                          "WHERE c.kind='memories' AND c.id LIKE 'fact-%' "
                          "AND (i.id IS NULL OR i.version<>c.version)").fetchall()
        for row in rows:
            record = store._decode(row["kind"], row["id"], row["version"], row["body"])
            write(store, db, row["kind"], record["data"], row["version"])


def catalog(governed, actor, account, conversation):
    store = governed.store
    if getattr(store, "backend", None) != "postgres":
        return [describe(store, row) for row in governed.records("memories")[0]
                if row["data"].get("fact") and row["data"]["id"].startswith("fact-")
                and tuple(row["data"]["fact"].get(k) for k in ("actor", "account", "conversation"))
                == (actor, account, conversation)]
    with store.db() as db:
        rows = db.execute("SELECT i.id,i.version,i.attribute,i.value_key,i.valid_from,i.supersedes "
                          "FROM personal_fact_index i JOIN content_records c USING(kind,id) "
                          "WHERE i.scope=? AND i.version=c.version ORDER BY i.stamp DESC,i.id",
                          (scope(store, actor, account, conversation),)).fetchall()
    roots = ["memories/" + row["id"] for row in rows]
    allowed = governed.repository.readable_resources(governed.access, governed.request, candidates=roots)
    return [{**dict(row), "supersedes": json.loads(row["supersedes"])} for row in rows
            if "memories/" + row["id"] in allowed]


def signature(rows):
    return hashlib.sha256(json.dumps(sorted(rows, key=lambda r: r["id"]), sort_keys=True).encode()).hexdigest()


def statuses(rows, now):
    active = [row for row in rows if datetime.fromisoformat(row["valid_from"]) <= now]
    retired = {identifier for row in active for identifier in row["supersedes"]}
    values = {}
    for row in active:
        if row["id"] not in retired:
            values.setdefault(row["attribute"], set()).add(row["value_key"])
    return {row["id"]: ("future" if datetime.fromisoformat(row["valid_from"]) > now else
                        "historical" if row["id"] in retired else
                        "disputed" if len(values.get(row["attribute"], ())) > 1 else "active") for row in rows}
