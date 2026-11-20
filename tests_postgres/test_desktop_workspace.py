import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from companion_core import context_workspace as workspace
from companion_core.learning_tree import ensure
from qiban.desktop_profile import desktop_profile
from tests_postgres.test_desktop_management import call
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service


@pytest.fixture
def pg_profile():
    return desktop_profile()


def read(manager, op, args=None):
    value = call(manager, op, args)
    assert value["ok"], value
    return value["result"]


def save(service, config):
    return read(service, "context.preset_save", {"id": uuid4().hex, "revision": None,
                "config": config, "name": "测试用方案"})["preset"]


def apply(service, preset, base):
    return read(service, "context.preset_apply", {"id": preset["id"], "revision": preset["revision"],
                "base": base})["workspace"]


def test_preview_apply_conflict_reset_and_frozen_reply(service):
    initial = read(service, "context.workspace")
    assert "context.publish" not in read(service, "workspace.capabilities")["operations"]
    config = {**initial["active"]["config"], "guidance": {"private": "请简短作答"}, "disabled": ["summary"]}
    item = save(service, config)
    preview = read(service, "context.preview_config", {"scene": "private", "config": config})
    assert preview["reports"]["draft"]["sections"]["summary"]["reason"] == "disabled"
    assert preview["reports"]["active"]["sections"]["summary"]["kept"] == 1
    assert read(service, "context.preview_config", {"scene": "public", "config": config})["excluded"]
    old = workspace.revision(service.store)
    published = apply(service, item, "initial")
    one = published["active"]["version"]
    assert old["config"]["guidance"] == {} and one != old["version"]
    assert apply(service, item, one)["active"]["version"] == one
    restored = apply(service, workspace.DEFAULT_PRESET, one)
    assert restored["active"]["parent"] == one and restored["active"]["config"] == old["config"]
    assert [v["version"] for v in read(service, "context.history", {"after": None})["items"]] == [
        restored["active"]["version"], one, "initial"]
    service.unlocked = False
    assert call(service, "context.workspace")["error"] == "desktop_locked"


@pytest.mark.asyncio
async def test_publish_during_desktop_reply_only_changes_the_following_reply(service):
    from companion_core.reply_trace import listing
    from qiban.desktop_runtime import DesktopRuntime
    from tests_postgres.test_runtime import Model

    config = {**workspace.DEFAULT, "guidance": {"private": "合成的局部规则"}}
    item = save(service, config)
    class Publishing(Model):
        async def generate(self, instructions, context, schema, images=(), **kwargs):
            if not self.contexts:
                self.version = apply(service, item, "initial")["active"]["version"]
            return await super().generate(instructions, context, schema, images, **kwargs)
    model = Publishing()
    runtime = DesktopRuntime(service, "workspace-test", room_model=model, adapters=[service.room])
    try:
        runtime.start("local")
        for _ in range(2):
            read(service, "room.post", {"session": service.session_id, "text": "请打个招呼", "nonce": uuid4().hex})
            event, = await service.room.runtime_poll()
            assert (await runtime.cores["local"].handle(event))["action"]["status"] == "succeeded"
        assert "application_reply_guidance" not in model.contexts[0]
        assert model.contexts[1]["application_reply_guidance"] == "合成的局部规则"
        versions = []
        for row in listing(service.store, 2):
            trace = read(service, "reply_trace.detail", {"identifier": row["id"]})
            versions.append(next(s["workspace_version"] for s in trace["steps"] if s["stage"] == "context_budget"))
        assert versions == [model.version, "initial"]
        assert runtime.status()["active_preset"]["id"] == item["id"]
        pages = [read(service, "context.preset_replies", {"id": key, "after": None})
                 for key in ("default", item["id"])]
        assert all(len(p["items"]) == 1 and p["items"][0]["text"] for p in pages)
        assert pages[0]["items"][0]["id"] != pages[1]["items"][0]["id"]
        with service.store.db() as db:
            db.execute("DELETE FROM content_records WHERE kind='conversation_turns' AND id=?", (pages[0]["items"][0]["id"],))
        assert read(service, "context.preset_replies", {"id": "default", "after": None})["items"] == []
    finally:
        runtime.close()


def goal_fixture(manager):
    from companion_core.public_workspace import PublicWorkspace

    store, now = PublicWorkspace(manager.store), datetime.now(timezone.utc)
    key = "goal-synthetic"
    record = store.write("learning_goals", {"schema_version": 1, "id": key, "account_uid": "123",
        "request": {"id": "synthetic-turn", "version": "a" * 64}, "request_quote": "请学习倾听",
        "objective": "学习倾听", "criteria": ["辨别请求"], "status": "active", "phase": "plan",
        "reason": "explicit_user_request", "gaps": [], "round": 1, "round_limit": 6, "knowledge_refs": [],
        "visited_bvids": [], "queries": [], "query": "", "selected_bvid": "", "check_ids": [],
        "updated_at": now.isoformat(), "source": {"type": "user_instruction", "reference": "synthetic-turn",
        "recorded_at": now.isoformat()}, "locked": False}, None, manager.actor.key, key)
    ensure(SimpleNamespace(store=store, clock=lambda: now, identity=lambda: manager.actor.key), record)
    manager.bili = SimpleNamespace(_account=lambda: {"uid": "123", "revision": "one"}, lock=threading.RLock())
    page = read(manager, "learning.goals", {"after": ""})
    return {"account": page["account"], "goal": key, "definition": page["items"][0]["definition"]}


def ready(manager, scope):
    for _ in range(80):
        snapshot = read(manager, "learning.snapshot", scope)
        if snapshot["ready"]:
            return snapshot["snapshot"]
        time.sleep(.025)
    pytest.fail(str(snapshot))


def test_learning_notes_snapshot_criteria_conflict_pause_and_account(service):
    scope = goal_fixture(service)
    read(service, "learning.index_control", {"paused": True})
    assert not read(service, "learning.snapshot", scope)["ready"]
    read(service, "learning.index_control", {"paused": False})
    base = {**scope, "snapshot": ready(service, scope)}
    first = read(service, "learning.tile", {**base, "x": 0, "y": 0, "level": 0, "offset": 0})["items"][0]
    assert read(service, "learning.detail", {**base, "node": first["id"]})["mastery"] is None
    key = "note-" + uuid4().hex
    edit = {**base, "node": key, "expected": None, "changes": {"title": "先倾听", "parent_id": first["id"]}}
    saved = read(service, "learning.edit", edit)
    updated = {**scope, "snapshot": ready(service, scope)}
    assert read(service, "learning.edit", edit) == saved
    assert call(service, "learning.edit", {**edit, "changes": {"title": "另一窗口"}})["error"] == "edit_conflict"
    assert read(service, "learning.tile", {**base, "x": 0, "y": 0, "level": 0, "offset": 0})["total"] == 1
    row = read(service, "learning.search", {**updated, "query": "倾听", "filter": "active", "offset": 0})["items"][0]
    assert row["id"] == key and row["x"] > first["x"]
    cycle = {**updated, "node": key, "expected": saved["version"], "changes": {"parent_id": key}}
    assert call(service, "learning.edit", cycle)["error"] == "learning_graph"
    read(service, "learning.edit", {**cycle, "changes": {"archived": True}})
    service.bili._account = lambda: {"uid": "456", "revision": "two"}
    assert call(service, "learning.snapshot", scope)["error"] == "learning_unavailable"
    assert not read(service, "learning.goals", {"after": ""})["items"]


def test_layout_checkpoint_resumes_last_complete_batch_and_discards_stale_data(desktop_store):
    from qiban.desktop_learning_checkpoint import checkpoint

    signature = "a" * 64
    row = {"id": "note-example", "type": "note", "body": "合成的私人笔记"}
    with pytest.raises(RuntimeError):
        with checkpoint(desktop_store, signature) as saved:
            saved.append({row["id"]: row}, [row], force=True)
            raise RuntimeError("simulate shutdown")
    path = desktop_store.runtime / "learning-layout.partial"
    assert row["body"].encode() not in path.read_bytes()
    with path.open("ab") as file:
        file.write(b"incomplete-frame")
    with pytest.raises(RuntimeError):
        with checkpoint(desktop_store, signature) as saved:
            recovered = {}
            assert saved.resume(recovered) == row["id"] and recovered[row["id"]] == row
            raise RuntimeError("simulate another shutdown")
    with checkpoint(desktop_store, "b" * 64) as saved:
        assert saved.resume({}) == ""
    assert not path.exists()
