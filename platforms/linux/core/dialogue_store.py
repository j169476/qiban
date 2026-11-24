"""Dedicated local fixture transport, counters and encrypted Companion store."""
import hashlib
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from companion_core.contracts import ActorRef, Event, stable_key
from companion_core.postgres import PostgresStore
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.storage import atomic_write

OPTIONS = {"host": "/run/postgresql", "dbname": "qiban_synthetic", "user": "qiban", "connect_timeout": 3}
ROOT = Path("/var/lib/qiban/core")
SCOPE = ReplyScope("qq", "synthetic-account", "private", "synthetic-peer")
REPLY = "已收到合成会话里的登记请求。"
RUN_KEY = "core-run:linux-synthetic"


def connection():
    return psycopg.connect(**OPTIONS, row_factory=dict_row)


def open_store(*, provision=False):
    key_path = ROOT / "ledger.key"
    if provision:
        ROOT.mkdir(mode=0o700, exist_ok=True)
        with connection() as db:
            existing = db.execute("SELECT to_regclass('schema_migrations') AS name").fetchone()["name"]
        if not key_path.exists() and existing is None:
            atomic_write(key_path, secrets.token_bytes(32))
    store = PostgresStore(ROOT, OPTIONS, key_path.read_bytes())
    if provision:
        store.migrate()
        if not store.initialize()["ok"]:
            raise RuntimeError("Invalid encrypted synthetic store")
        if not store.state("linux-fixture-policy"):
            with store.lock, store.db() as db:
                policy = store.read("policy")
                store.write("policy", {**policy["data"], "daily_action_limit": 1000, "daily_model_cycles": 1000},
                            policy["version"], "linux-fixture")
                store.set_state("linux-fixture-policy", True, db)
    return store


def initialize_transport():
    with connection() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS lab_messages (
                id text PRIMARY KEY, mode text NOT NULL, occurred text NOT NULL, permit text,
                model_calls int NOT NULL DEFAULT 0, in_model boolean NOT NULL DEFAULT false,
                released boolean NOT NULL DEFAULT false);
            CREATE TABLE IF NOT EXISTS lab_inputs (
                id bigserial PRIMARY KEY, message text NOT NULL REFERENCES lab_messages,
                outcome text NOT NULL DEFAULT 'queued', error text, action text);
            CREATE INDEX IF NOT EXISTS lab_inputs_pending ON lab_inputs(id) WHERE outcome='queued';
            CREATE TABLE IF NOT EXISTS lab_receiver (
                seq bigserial PRIMARY KEY, message text NOT NULL REFERENCES lab_messages,
                target text NOT NULL, body text NOT NULL, receipt text NOT NULL);
            """, prepare=False)
        db.execute("UPDATE lab_inputs SET outcome='queued' WHERE outcome='processing'")


def authorize(store, enabled):
    auth = ReplyAuthorizations(store)
    current = auth.record(SCOPE)
    return auth.update(SCOPE, enabled=enabled, expected_version=current["version"], actor="linux-fixture-admin")


def submit(store, mode):
    identifier = uuid.uuid4().hex
    # Bind the actual admission version to the immutable synthetic inbound message.
    with store.lock, connection() as db:
        permit = ReplyAuthorizations(store).version(SCOPE)
        db.execute("INSERT INTO lab_messages(id,mode,occurred,permit) VALUES (%s,%s,%s,%s)",
                   (identifier, mode, datetime.now(timezone.utc).isoformat(), permit))
        queue = db.execute("INSERT INTO lab_inputs(message) VALUES (%s) RETURNING id", (identifier,)).fetchone()
    return {"message": identifier, "input": queue["id"]}


def replay(identifier):
    with connection() as db:
        result = db.execute("INSERT INTO lab_inputs(message) VALUES (%s) RETURNING id", (identifier,)).fetchone()
    return {"message": identifier, "input": result["id"]}


def make_event(row):
    return Event("qq", SCOPE.account_identity, row["id"], ActorRef("qq", SCOPE.target_id), "fixture-private",
                 row["occurred"], "请登记这条合成会话。", "private",
                 metadata={"qq": {"target_id": SCOPE.target_id, "identity_quality": "verified"},
                           "fixture_permit": row["permit"]})


def progress():
    with connection() as db:
        row = db.execute("""
            SELECT (SELECT count(*) FROM core_actions WHERE account=%s AND status='succeeded') AS confirmed,
                   (SELECT count(*) FROM core_actions WHERE account=%s AND status='uncertain') AS uncertain,
                   (SELECT count(*) FROM lab_receiver) AS submissions,
                   count(*) FILTER (WHERE outcome='denied') AS denied,
                   count(*) FILTER (WHERE outcome='duplicate') AS replays FROM lab_inputs
            """, (SCOPE.account_identity, SCOPE.account_identity)).fetchone()
    return {"backend": "postgresql", "mode": "synthetic", **row}


def inspect_message(store, identifier):
    with connection() as db:
        record = db.execute("SELECT * FROM lab_messages WHERE id=%s", (identifier,)).fetchone()
        if not record:
            raise ValueError("Unknown synthetic message")
        inputs = db.execute("SELECT id,outcome,error,action FROM lab_inputs WHERE message=%s ORDER BY id",
                            (identifier,)).fetchall()
        submitted = db.execute("SELECT target,body,receipt FROM lab_receiver WHERE message=%s ORDER BY seq",
                               (identifier,)).fetchall()
    event = make_event(record)
    dedupe = stable_key("dispatch", event.platform, event.account, event.id, "message.reply")
    with store.db() as db:
        ledger = db.execute("SELECT id,status,result FROM core_actions WHERE dedupe=?", (dedupe,)).fetchone()
        ingress = db.execute("SELECT version FROM core_events WHERE id=?", (event.id,)).fetchone()
    action = None
    if ledger:
        detail = store.ledger_decode("core_actions.result", ledger["result"]) if ledger["result"] else {}
        action = {"id": ledger["id"], "status": ledger["status"], "receipt": detail.get("receipt", {})}
    return {"message": identifier, "mode": record["mode"], "in_model": record["in_model"],
            "model_calls": record["model_calls"], "event": event.id, "event_version": ingress[0] if ingress else None,
            "processed_version": store.state("core-processed:" + event.id), "inputs": inputs, "action": action,
            "receiver": [{"target": r["target"], "reply_sha256": hashlib.sha256(r["body"].encode()).hexdigest(),
                          "receipt": r["receipt"]} for r in submitted]}
