"""Bounded reclamation of ordinary chat and diagnostic history in the desktop database."""

from datetime import datetime, timedelta, timezone

from .contracts import stable_key
from .source_retention import INDEX, held

HISTORY_BYTES = 10_000_000_000
HISTORY_DAYS = 60


def intake_paused(store):
    with store.db() as db:
        size = db.execute("SELECT bytes FROM ordinary_history_total WHERE singleton").fetchone()[0]
    return size > HISTORY_BYTES


def referenced(db, resource):
    return (
        held(db, resource)
        or db.execute(
            """SELECT 1 WHERE
        EXISTS(SELECT 1 FROM source_dependencies WHERE source=?) OR
        EXISTS(SELECT 1 FROM grant_resources WHERE resource=?) OR
        EXISTS(SELECT 1 FROM governance_provenance WHERE target=?) OR
        EXISTS(SELECT 1 FROM governance_challenges WHERE jsonb_exists(resources::jsonb,?))
        """,
            (resource,) * 4,
        ).fetchone()
        is not None
    )


def consumed(store, db, identifier, version):
    receipt = db.execute("SELECT version FROM core_event_receipts WHERE event_id=?", (identifier,)).fetchone()
    return bool(
        receipt
        and receipt[0] == version
        or store.state("merged-consumed:" + identifier, {}, db).get("version") == version
    )


def remove_event(store, db, identifier, now):
    row = db.execute(
        "SELECT account,platform,version,body,deleted FROM core_events WHERE id=? AND NOT history_pruned",
        (identifier,),
    ).fetchone()
    if not row or not row["deleted"] and not consumed(store, db, identifier, row["version"]):
        return False
    resource = "event/" + identifier
    event = store.ledger_decode("core_events.body", row["body"])
    ids = [identifier, event["native_id"], resource]
    if row["platform"] == "bilibili":
        with db.stream(
            "SELECT payload FROM actions WHERE account=? AND status IN "
            "('prepared','dispatching','uncertain')",
            (row["account"],),
        ) as pending:
            if any(
                store.ledger_decode("actions.payload", action[0]).get("event_id") in ids for action in pending
            ):
                return False
    with db.stream(
        "SELECT body FROM core_actions WHERE account=? AND platform=? AND status IN "
        "('prepared','dispatching','uncertain')",
        (row["account"], row["platform"]),
    ) as pending:
        if any(
            store.ledger_decode("core_actions.body", action[0]).get("event", {}).get("id") == identifier
            for action in pending
        ):
            return False
    if (
        referenced(db, resource)
        or db.execute(
            """SELECT 1 WHERE
            EXISTS(SELECT 1 FROM qq_native_inbox WHERE event_id=?) OR
            EXISTS(SELECT 1 FROM governance_grants WHERE evidence_id=ANY(?)) OR
            EXISTS(SELECT 1 FROM relationship_pending WHERE event_id=ANY(?)) OR
            EXISTS(SELECT 1 FROM content_records WHERE source_ref=ANY(?)) OR
            EXISTS(SELECT 1 FROM input_image_blobs WHERE event_id=?) OR
            EXISTS(SELECT 1 FROM media_originals m WHERE event_id=? AND
              (preserved OR status<>'expired' OR expired_at IS NULL OR expired_at>? OR
               EXISTS(SELECT 1 FROM media_original_holds h WHERE h.asset=m.id)))""",
            (identifier, ids, ids, ids, identifier, identifier, now.timestamp() - 90 * 86400),
        ).fetchone()
    ):
        return False
    batch_key = store.state("merged-anchor:" + identifier, db=db)
    if batch_key:
        batch = store.state(batch_key, {}, db)
        if batch.get("status") == "pending":
            return False
        if batch.get("status") in ("completed", "superseded"):
            active = "merged-active:" + batch["thread"]
            if store.state(active, db=db) == batch_key:
                db.execute("DELETE FROM state WHERE key=?", (active,))
            db.execute("DELETE FROM state WHERE key IN (?,?)", (batch_key, "merged-anchor:" + identifier))
    # Keep the version receipt after deleting both representations of this processed event.
    # An exact replay can be recognized even when the provider resends raw history.
    if not row["deleted"]:
        store.set_state("core-processed:" + identifier, row["version"], db)
    if row["platform"] == "bilibili":
        db.execute("DELETE FROM events WHERE account=? AND id=?", (row["account"], event["native_id"]))
    db.execute("DELETE FROM media_originals WHERE event_id=?", (identifier,))
    if row["deleted"]:
        db.execute(
            "UPDATE core_events SET body=?,history_pruned=true WHERE id=?",
            (store.ledger_encode("core_events.body", {}), identifier),
        )
    else:
        db.execute("DELETE FROM core_events WHERE id=?", (identifier,))
        db.execute("DELETE FROM governance_policies WHERE resource=?", (resource,))
    return True


def prune(store, db, row, now):
    table, key, second, third = (row[name] for name in ("family", "k1", "k2", "k3"))
    if table in ("content_records", "content_history"):
        resource = key + "/" + second
        if (
            referenced(db, resource)
            or db.execute(
                "SELECT 1 FROM content_records WHERE (kind=? AND id=? AND locked) OR source_ref=ANY(?) LIMIT 1",
                (key, second, [second, resource]),
            ).fetchone()
        ):
            return False
        if table == "content_records":
            db.execute("DELETE FROM content_records WHERE kind=? AND id=? AND NOT locked", (key, second))
            db.execute("DELETE FROM governance_policies WHERE resource=?", (resource,))
        else:
            db.execute(
                "DELETE FROM content_history WHERE kind=? AND id=? AND version=?", (key, second, third)
            )
    elif table == "core_events":
        return remove_event(store, db, key, now)
    elif table == "events":
        return remove_event(store, db, stable_key("event", "bilibili", key, second), now)
    elif table in ("content_audit", "governance_audit"):
        db.execute("DELETE FROM " + table + " WHERE id=?", (int(key) if table == "content_audit" else key,))
    elif table in ("actions", "core_actions"):
        status = db.execute(f"SELECT status FROM {table} WHERE id=?", (key,)).fetchone()[0]
        if status not in {"succeeded", "confirmed", "failed", "cancelled"}:
            return False
        column = "payload" if table == "actions" else "body"
        if table == "actions":
            kind = db.execute("SELECT kind FROM actions WHERE id=?", (key,)).fetchone()[0]
            if kind in ("reply", "dm_reply", "comment") and not store.state(
                "conversation-action-archived:" + key, db=db
            ):
                return False
        if table == "core_actions":
            data = store.ledger_decode(
                "core_actions.body",
                db.execute("SELECT body FROM core_actions WHERE id=?", (key,)).fetchone()[0],
            )
            event = data.get("event", {})
            if not event or not event.get("native_id"):
                return False
            event_id = stable_key("event", event["platform"], event["account"], event["native_id"])
            if not (
                store.state("core-processed:" + event_id, db=db)
                or store.state("merged-consumed:" + event_id, db=db)
            ):
                return False
        # Quota and dedupe queries keep their original indexed fields and outcome.
        # Today's records may still be needed to reconstruct per-recipient quotas.
        if row["stamp"].date() >= now.date():
            return False
        db.execute(
            f"UPDATE {table} SET {column}=?,result=NULL,history_pruned=true WHERE id=?",
            (store.ledger_encode(table + "." + column, {}), key),
        )
        if table == "core_actions":
            db.execute("DELETE FROM source_retention WHERE owner_kind='core_action' AND owner_id=?", (key,))
        else:
            db.execute("DELETE FROM state WHERE key=?", ("action-provenance:" + key,))
    else:
        raise ValueError("unrecognized history family")
    return True


def cleanup(store, *, guard=lambda: None, now=None, capacity=HISTORY_BYTES, limit=100):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=HISTORY_DAYS)
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        guard()

        def total():
            return db.execute("SELECT bytes FROM ordinary_history_total WHERE singleton").fetchone()[0]

        size = total()
        if store.state(INDEX, {}, db).get("stage") != "ready":
            from .source_retention import backfill

            ready = backfill(store, db)
            if not ready and store.state(INDEX, {}, db).get("stage") == "state":
                ready = backfill(store, db)
            if not ready:
                return {"removed": 0, "bytes": size, "over_capacity": size > capacity, "indexing": True}
        rows = db.execute(
            """SELECT * FROM ordinary_history WHERE (retry_at IS NULL OR retry_at<=?)
            AND (stamp<=? OR ?) ORDER BY stamp,family,k1,k2,k3 LIMIT ?""",
            (now, cutoff, size > capacity, limit),
        ).fetchall()
        removed = 0
        for row in rows:
            if row["stamp"] > cutoff and size <= capacity:
                break
            guard()
            family_key = tuple(row[k] for k in ("family", "k1", "k2", "k3"))
            # An event can retire its legacy mirror in the same transaction.
            if not db.execute(
                "SELECT 1 FROM ordinary_history WHERE (family,k1,k2,k3)=(?,?,?,?)", family_key
            ).fetchone():
                continue
            if prune(store, db, row, now):
                removed += 1
                size = total()
            else:
                db.execute(
                    "UPDATE ordinary_history SET retry_at=? WHERE (family,k1,k2,k3)=(?,?,?,?)",
                    (now + timedelta(hours=6), *family_key),
                )
        guard()
        return {"removed": removed, "bytes": size, "over_capacity": size > capacity}
