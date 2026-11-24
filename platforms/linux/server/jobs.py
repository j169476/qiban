"""Local acceptance workload. All indexed documents come from this built-in fixture."""
import argparse
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from companion_core import passages

DB = Path("/var/lib/qiban/tasks.sqlite3")
FIXTURE = "\n".join(f"Survey note {i}: a lantern marks the river crossing. 夜间调查记录，河岸设有路标。"
                    for i in range(1, 65))


def connect():
    return sqlite3.connect(DB, timeout=3)


def initialize(db):
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS installation(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, due REAL NOT NULL, state TEXT NOT NULL,
            source TEXT NOT NULL, finished REAL, digest TEXT);
        CREATE INDEX IF NOT EXISTS due_jobs ON jobs(state,due);
        CREATE VIRTUAL TABLE IF NOT EXISTS sections USING fts5(job UNINDEXED, ordinal UNINDEXED,
            start UNINDEXED, end UNINDEXED, body);
    """)
    with db:
        db.execute("INSERT INTO installation SELECT ? WHERE NOT EXISTS (SELECT 1 FROM installation)",
                   (uuid.uuid4().hex,))
        # The only worker holds an exclusive lifetime flock. Any prior claim was interrupted.
        db.execute("UPDATE jobs SET state='queued' WHERE state='running'")
    return db.execute("SELECT id FROM installation").fetchone()[0]


def enqueue(db, delay):
    identifier = uuid.uuid4().hex
    with db:
        db.execute("INSERT INTO jobs(id,due,state,source) VALUES(?,?,'queued',?)",
                   (identifier, time.time() + delay, FIXTURE))
    return identifier


def work_one(db):
    row = db.execute("SELECT id,source FROM jobs WHERE state='queued' AND due<=? ORDER BY due,id LIMIT 1",
                     (time.time(),)).fetchone()
    if row is None:
        return
    identifier, source = row
    with db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (identifier,))
    try:
        spans = passages.split(source)
        if not passages.valid(spans, len(source)):
            raise ValueError("Invalid source spans")
        # Completion and all index rows commit together. Recovery can retry an uncommitted job.
        with db:
            db.executemany("INSERT INTO sections(job,ordinal,start,end,body) VALUES(?,?,?,?,?)",
                           ((identifier, ordinal, start, end, source[start:end])
                            for ordinal, (start, end) in enumerate(spans)))
            db.execute("UPDATE jobs SET state='completed',finished=?,digest=? WHERE id=?",
                       (time.time(), hashlib.sha256(source.encode()).hexdigest(), identifier))
    except ValueError:
        with db:
            db.execute("UPDATE jobs SET state='failed',finished=? WHERE id=?", (time.time(), identifier))


def progress(db):
    row = db.execute("""
        SELECT count(*) FILTER (WHERE state='queued'), count(*) FILTER (WHERE state='running'),
               count(*) FILTER (WHERE state='completed'), count(*) FILTER (WHERE state='failed'),
               (SELECT id FROM jobs WHERE state='completed' ORDER BY finished DESC,rowid DESC LIMIT 1)
        FROM jobs
    """).fetchone()
    return dict(zip(("queued", "running", "completed", "failed", "last_completed_id"), row, strict=True))


def verify(db, identifier):
    row = db.execute("SELECT state,source,digest FROM jobs WHERE id=?", (identifier,)).fetchone()
    if not row or row[0] != "completed" or row[1] != FIXTURE:
        raise ValueError("Expected a completed fixture job")
    spans = passages.split(FIXTURE)
    rows = db.execute("SELECT start,end,body FROM sections WHERE job=? ORDER BY ordinal", (identifier,)).fetchall()
    hits = db.execute("SELECT count(*) FROM sections WHERE sections MATCH 'lantern' AND job=?",
                      (identifier,)).fetchone()[0]
    if (rows != [(a, b, FIXTURE[a:b]) for a, b in spans] or hits < 1 or
            row[2] != hashlib.sha256(FIXTURE.encode()).hexdigest()):
        raise ValueError("Fixture index did not match its source")
    return {"id": identifier, "spans": len(rows), "search_hits": hits, "sha256": row[2],
            "algorithm": passages.REVISION}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("enqueue").add_argument("--delay", type=int, choices=range(0, 601), metavar="0..600", default=3)
    actions.add_parser("verify").add_argument("id")
    args = parser.parse_args()
    with closing(connect()) as db:
        result = {"id": enqueue(db, args.delay)} if args.action == "enqueue" else verify(db, args.id)
    print(json.dumps(result))
