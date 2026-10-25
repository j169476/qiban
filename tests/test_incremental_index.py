import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from bili_assistant.storage import Store


def seed(store, count=20):
    for i in range(count):
        uid = str(1000 + i)
        store.write("relationships", {"schema_version": 1, "id": uid, "display_name": "合成用户",
                    "affinity": 50, "interactions": 1, "events": ["seed"], "locked": False,
                    "impression": "开源机器人"}, None, "test", uid)
    store.context("机器人")


def test_restart_reuses_index_and_only_reparses_changed_bytes(store):
    seed(store)
    reopened = Store(store.root)
    assert reopened.context("机器人")["total_records"] == 20
    assert reopened.index_stats["parsed"] == 0
    assert reopened.index_stats["persistent_hits"] == 20
    path = store.path("relationships", "1000")
    stat = path.stat()
    path.write_bytes(path.read_bytes().replace(b"affinity: 50", b"affinity: 70"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    changed = Store(store.root)
    result = changed.context("机器人")
    assert next(r for r in result["records"] if r["data"]["id"] == "1000")["data"]["affinity"] == 70
    assert changed.index_stats["parsed"] == 1 and changed.index_stats["persistent_hits"] == 19
    store.path("relationships", "1001").unlink()
    store.path("relationships", "1002").write_text("bad: [", encoding="utf-8")
    latest = Store(store.root)
    assert latest.context()["total_records"] == 18
    with closing(sqlite3.connect(store.root / "cache/parsed.sqlite3")) as db:
        assert db.execute("SELECT COUNT(*) FROM parsed WHERE id IN ('1001','1002')").fetchone()[0] == 0


def test_corrupt_cache_falls_back_to_yaml_and_can_be_rebuilt(store):
    seed(store)
    path = store.root / "cache/parsed.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE parsed SET data=? WHERE id='1000'", (json.dumps({"id": "1000", "affinity": 99}),))
    reopened = Store(store.root)
    assert reopened.read("relationships", "1000")["data"]["affinity"] == 50
    path.write_bytes(b"corrupted database")
    broken = Store(store.root)
    assert broken.context()["total_records"] == 20
    assert broken.index_stats["cache_errors"] > 0
    assert broken.rebuild_index()["indexed"] == 20
    rebuilt = Store(store.root)
    assert rebuilt.context()["total_records"] == 20
    assert rebuilt.index_stats["persistent_hits"] == 20


def test_uid_context_does_not_enumerate_unrelated_relationships(store, monkeypatch):
    seed(store, 50)
    reopened = Store(store.root)
    original = reopened.records

    def guarded(kind):
        assert kind != "relationships"
        return original(kind)

    monkeypatch.setattr(reopened, "records", guarded)
    result = reopened.context("机器人", uids=["1000"], references=[{"kind": "relationships", "id": "1001"}])
    assert {r["data"]["id"] for r in result["records"]} == {"1000", "1001"}
    assert result["relationship_scope"] == {"mode": "uids", "uids": ["1000", "1001"]}
    assert reopened.index_stats["persistent_hits"] == 2


def test_independent_readers_can_build_same_disposable_index(store):
    seed(store)
    from bili_assistant.index_cache import reset

    reset(store)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: Store(store.root).context()["total_records"], range(4)))
    assert results == [20] * 4
    assert Store(store.root).context()["total_records"] == 20
