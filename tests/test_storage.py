import json
import zipfile

import pytest

from bili_assistant.errors import AssistantError
from bili_assistant.storage import Store


def test_init_is_non_destructive(store):
    doc = store.read("persona")
    doc["data"]["name"] = "用户自定义名字"
    store.write("persona", doc["data"], doc["version"], "user")
    store.initialize()
    assert store.read("persona")["data"]["name"] == "用户自定义名字"
    assert store.validate_all()["ok"]


def test_edit_conflict_preserves_user_file(store):
    old = store.read("persona")
    current = store.read("persona")
    current["data"]["name"] = "人工版本"
    store.write("persona", current["data"], current["version"], "user")
    old["data"]["name"] = "过期自动版本"
    with pytest.raises(AssistantError, match="本地版本"):
        store.write("persona", old["data"], old["version"], "role")
    assert store.read("persona")["data"]["name"] == "人工版本"
    assert list((store.root / "conflicts").glob("*.yaml"))


def test_locked_memory_cannot_be_overwritten(store, memory):
    memory["locked"] = True
    saved = store.write("memories", memory, None, "user", "m1")
    memory["content"] = "自动改写"
    with pytest.raises(AssistantError, match="锁定"):
        store.write("memories", memory, saved["version"], "role", "m1")


@pytest.mark.parametrize(
    "raw",
    [
        "name: one\nname: two\n",
        "!!python/object/apply:os.system ['echo dangerous']",
        "schema_version: 1\nname: [bad\n",
        "schema_version: 1\nwhen: 2026-10-20\n",
    ],
)
def test_invalid_yaml_preserved(store, raw):
    path = store.path("persona")
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(AssistantError):
        store.read("persona")
    assert path.read_text(encoding="utf-8") == raw


def test_path_escape_rejected(store, memory):
    with pytest.raises(AssistantError):
        store.write("memories", memory, None, "user", "../policy")


def test_delete_removes_search_result(store, memory):
    store.write("memories", memory, None, "user", "m1")
    assert len(store.search("实际案例")) == 1
    store.path("memories", "m1").unlink()
    assert not store.search("实际案例")


def test_rebuild_recovers_corrupt_index(store, memory):
    store.write("memories", memory, None, "user", "m1")
    store.rebuild_index()
    (store.root / "cache" / "search.sqlite3").write_bytes(b"corrupted")
    assert len(store.search("案例")) == 1


def test_export_has_only_canonical_data(store, memory, tmp_path):
    store.write("memories", memory, None, "user", "m1")
    (store.runtime / "do-not-export.json").write_text(json.dumps({"private": True}))
    target = tmp_path / "export.zip"
    store.export(target)
    with zipfile.ZipFile(target) as z:
        names = z.namelist()
    assert "memories/m1.yaml" in names
    assert not any(n.startswith(("runtime/", "cache/", "history/")) for n in names)


def test_persist_and_invalid_note_isolation(store, memory):
    store.write("memories", memory, None, "user", "m1")
    (store.root / "memories" / "broken.yaml").write_text("bad: [")
    reopened = Store(store.root)
    assert len(reopened.records("memories")[0]) == 1
    assert len(reopened.records("memories")[1]) == 1


def test_critical_policy_invalid_reported(store):
    policy = store.read("policy")
    policy["data"]["timezone"] = "No/SuchZone"
    with pytest.raises(AssistantError):
        store.write("policy", policy["data"], policy["version"], "admin")
