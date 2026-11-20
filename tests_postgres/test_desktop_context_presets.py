"""Named workspace settings must survive restart, restore and conflicting editors."""
from uuid import uuid4

import pytest

from companion_core import context_workspace as workspace
from companion_core import postgres_transfer
from qiban.desktop_management import DesktopManagement
from qiban.desktop_profile import desktop_profile
from tests_postgres.test_desktop_management import call
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service


@pytest.fixture
def pg_profile():
    return desktop_profile()


def read(manager, op, args):
    response = call(manager, op, args)
    assert response["ok"], response
    return response["result"]


def pins(view):
    return {"base": view["active"]["version"]}


def test_save_apply_update_reset_and_restart(service):
    view = read(service, "context.workspace", {})
    config = {**view["active"]["config"], "guidance": {"private": "合成自定义场景"}}
    create = {"id": uuid4().hex, "name": "  合成方案  ", "revision": None,
              "config": config}
    item = read(service, "context.preset_save", create)
    preset = item["preset"]
    assert preset["name"] == "合成方案" and item["workspace"] == view
    assert read(service, "context.presets", {"after": None}) == {
        "items": [{"id": create["id"], "revision": preset["revision"], "name": preset["name"]}], "after": None}
    readback = read(service, "context.preset_read", {"id": create["id"]})
    assert readback == preset
    apply = {"id": preset["id"], "revision": preset["revision"], **pins(view)}
    view = read(service, "context.preset_apply", apply)["workspace"]
    assert view["active"]["config"] == config and view["active"]["preset"]["id"] == create["id"]
    assert call(service, "context.preset_apply", apply)["error"] == "edit_conflict"
    updated = {**create, "revision": preset["revision"],
               "config": {**config, "disabled": ["growth"]}}
    saved = read(service, "context.preset_save", updated)
    assert saved["workspace"]["active"]["config"] == config
    assert saved["preset"]["revision"] != preset["revision"]
    assert call(service, "context.preset_save", updated)["error"] == "edit_conflict"
    reset = read(service, "context.preset_apply", {
        "id": "default", "revision": "initial", **pins(saved["workspace"])})
    assert reset["workspace"]["active"]["config"] == workspace.DEFAULT
    assert workspace.revision(service.store, view["active"]["version"])["config"] == config
    restarted = DesktopManagement(service.store, "installation-test", "S-1-5-21-test-owner")
    assert read(restarted, "context.preset_read", {"id": create["id"]}) == saved["preset"]


def test_rejects_invalid_config_and_locked_or_restoring_writes(service):
    view = read(service, "context.workspace", {})
    empty = {"id": uuid4().hex, "name": "合成方案", "revision": None,
             "config": {}}
    assert call(service, "context.preset_save", empty)["error"] == "workspace_config"
    assert read(service, "context.presets", {"after": None})["items"] == []
    assert read(service, "context.workspace", {}) == view
    service.store.set_state("desktop-last-restore", {"status": "assets_pending"})
    assert call(service, "context.preset_save", empty)["error"] == "restore_pending"
    service.unlocked = False
    assert call(service, "context.preset_read", {"id": "default"})["error"] == "desktop_locked"


def test_encrypted_preset_included_in_profile_backup(service):
    store = service.store
    create = {"id": uuid4().hex, "name": "PRIVATE-PRESET", "revision": None, "config": workspace.DEFAULT}
    preset = read(service, "context.preset_save", create)["preset"]
    key = "reply-workspace:preset:" + preset["id"]
    with store.db() as db:
        raw = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()[0]
        assert raw.startswith("enc1:") and preset["name"] not in raw
    backup = postgres_transfer.create(store, store._test_key)
    read(service, "context.preset_save", {**create, "revision": preset["revision"], "name": "LATER"})
    postgres_transfer.restore(store, backup, store._test_key, expected=postgres_transfer.fingerprint(store))
    assert read(service, "context.preset_read", {"id": preset["id"]}) == preset


def test_upgrade_preserves_old_active_and_unapplied_draft_once(service):
    store = service.store
    version = uuid4().hex
    pending = {**workspace.DEFAULT, "guidance": {"private": "原稿"}}
    store.set_state(workspace.HEAD, version)
    store.set_state(workspace.PREFIX + version, {"version": version, "parent": "initial", "config": workspace.DEFAULT})
    store.set_state("reply-workspace:draft", {"base": version, "revision": uuid4().hex, "config": pending})
    for _ in range(2):
        store.migrate()
    items = read(service, "context.presets", {"after": None})["items"]
    assert sorted(p["name"] for p in items) == ["升级前方案", "升级前草稿"]
    active = read(service, "context.workspace", {})["active"]
    assert active["version"] == version and active["preset"] in items
    old_draft = next(p for p in items if p["name"] == "升级前草稿")
    assert read(service, "context.preset_read", {"id": old_draft["id"]})["config"] == pending
    assert store.state("reply-workspace:draft") is None


def test_distinct_presets_with_equal_content_and_record_pagination(service):
    from companion_core import conversations

    store = service.store
    view = read(service, "context.workspace", {})
    item = read(service, "context.preset_save", {"id": uuid4().hex, "name": "同内容", "revision": None,
                "config": workspace.DEFAULT})["preset"]
    applied = read(service, "context.preset_apply", {"id": item["id"], "revision": item["revision"], **pins(view)})
    assert applied["workspace"]["active"]["preset"]["id"] == item["id"]
    stamp = "2026-11-21T09:00:00+00:00"
    for i in range(23):
        turn, = conversations.append(store, "123", "codex", "local:one", str(i), str(i), "assistant",
                                     "123", "sent", stamp, sequence=i)
        with store.db() as db:
            db.execute("INSERT INTO preset_reply_turns(id,preset_id,sent_at) VALUES(?,?,?)", (turn, item["id"], stamp))
    query = {"id": item["id"], "after": None}
    first = read(service, "context.preset_replies", query)
    last = read(service, "context.preset_replies", {**query, "after": first["after"]})
    assert len(first["items"]) == 20 and len(last["items"]) == 3 and last["after"] is None
    assert {r["text"] for r in first["items"] + last["items"]} == {str(i) for i in range(23)}
    assert not read(service, "context.preset_replies", {**query, "id": "default"})["items"]
