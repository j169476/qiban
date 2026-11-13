"""Favorite tools use synthetic SDK responses and never contact Bilibili."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bilibili_api import Credential, favorite_list, video
from bilibili_api.utils import network

from bili_assistant.bilibili import Bilibili
from bili_assistant.errors import AssistantError
from bili_assistant.favorites import FavoriteTools

BV1, BV2 = "BV1RT4y1h7YY", "BV1xx411c7mD"


def row(fid, *, attr=2, title="分类", count=1, mid=123):
    return {"id": fid, "mid": mid, "attr": attr, "title": title, "media_count": count, "fav_state": 0}


@pytest.fixture
def kit(monkeypatch):
    def no_network():
        pytest.fail("Favorite-tool fixtures must not access the network")
    monkeypatch.setattr(network, "get_client", no_network)
    rows = [row(10, attr=1, title="改过名的默认夹"), row(20, title="公开分类")]
    states = {BV1: {10}, BV2: {10}}
    credential = Credential(sessdata="fixture-only", bili_jct="fixture", dedeuserid="123")
    adapter = Bilibili(SimpleNamespace(read=lambda _: {"data": {"account_uid": "123"}}))
    monkeypatch.setattr(adapter, "_credential", lambda: credential)
    # No retries / delays are needed for synthetic reads.
    async def read(operation):
        return await operation()
    monkeypatch.setattr(adapter, "_read", read)
    async def listing(uid, video=None, credential=None):
        result = copy.deepcopy(rows)
        if video:
            for item in result:
                item["fav_state"] = int(item["id"] in states.get(video.get_bvid(), set()))
        return {"count": len(result), "list": result}
    monkeypatch.setattr(favorite_list, "get_video_favorite_list", listing)
    tool = FavoriteTools(adapter, "123", lambda: None)
    return SimpleNamespace(tool=tool, rows=rows, states=states, adapter=adapter, credential=credential)


async def test_default_uses_flag_not_name_or_order(kit):
    kit.rows.reverse()
    kit.rows[0]["title"] = "默认收藏夹"
    assert await kit.tool.default() == 10
    kit.rows.append(row(30, attr=0))
    with pytest.raises(AssistantError, match="唯一"):
        await kit.tool.default()


async def test_catalog_bounded_pagination_and_changes(kit):
    kit.rows[:] = [row(i, title="夹" + str(i)) for i in range(1, 42)]
    first = await kit.tool.list()
    assert len(first["folders"]) == 20 and first["next_offset"] == 20
    last = await kit.tool.list(offset=40, expected=first["revision"])
    assert len(last["folders"]) == 1 and last["next_offset"] is None
    kit.rows[0]["attr"] = 3
    with pytest.raises(AssistantError, match="改变"):
        await kit.tool.list(offset=20, expected=first["revision"])


@pytest.mark.parametrize("bad", [None, {}, {"count": 1, "list": []},
                                  {"count": 1, "list": [row(1, mid=999)]},
                                  {"count": 2, "list": [row(1), row(1)]}])
async def test_incomplete_or_foreign_catalog_rejected(kit, monkeypatch, bad):
    monkeypatch.setattr(favorite_list, "get_video_favorite_list", AsyncMock(return_value=bad))
    with pytest.raises(AssistantError):
        await kit.tool.default()


async def test_contents_returns_bounded_metadata_only(kit, monkeypatch):
    raw = {"info": {"id": 10, "upper": {"mid": 123}}, "has_more": True,
           "medias": [{"bvid": BV1, "type": 2, "title": "标题" * 500, "intro": "简介" * 500,
                       "attr": 0, "secret_unused": "not-for-model"},
                      {"type": 24, "id": 111, "title": "not video"}]}
    getter = AsyncMock(return_value=raw)
    monkeypatch.setattr(favorite_list, "get_video_favorite_list_content", getter)
    result = await kit.tool.contents(10, keyword="分类")
    assert result["next_page"] == 2 and result["omitted"] == 1
    assert len(result["items"][0]["title"]) == 160
    assert len(result["items"][0]["description"]) == 240
    assert "not-for-model" not in str(result)
    assert getter.call_args.kwargs["keyword"] == "分类"
    raw["info"]["upper"]["mid"] = 999
    with pytest.raises(AssistantError):
        await kit.tool.contents(10)


@pytest.mark.parametrize("private", [True, False])
async def test_create_explicit_privacy_and_existing_dedup(kit, monkeypatch, private):
    async def post(operation, data, guard):
        guard()
        assert operation == "new" and data["privacy"] == int(private)
        kit.rows.append(row(30, title=data["title"], attr=2 + int(private)))
        return {"id": 30, "credential": "never-return"}
    post = AsyncMock(side_effect=post)
    monkeypatch.setattr(kit.tool, "_post", post)
    result = await kit.tool.create("知识", private, before_send=lambda: None)
    assert result["verified"] and result["folder"]["private"] == private
    assert "never-return" not in str(result)
    assert (await kit.tool.create("知识", private, before_send=lambda: None))["already_done"]
    assert post.await_count == 1
    with pytest.raises(AssistantError, match="同名"):
        await kit.tool.create("知识", not private, before_send=lambda: None)


async def test_create_missing_id_or_failed_read_is_uncertain(kit, monkeypatch):
    post = AsyncMock(return_value={})
    monkeypatch.setattr(kit.tool, "_post", post)
    with pytest.raises(AssistantError) as error:
        await kit.tool.create("知识", False, before_send=lambda: None)
    assert error.value.code == "action_unconfirmed" and post.await_count == 1
    post.return_value = {"id": 500}
    with pytest.raises(AssistantError) as error:
        await kit.tool.create("知识", False, before_send=lambda: None)
    assert error.value.code == "action_unconfirmed"


async def test_move_private_to_public_in_one_request_and_verify(kit, monkeypatch):
    async def post(operation, data, guard):
        guard()
        assert operation == "content_move"
        assert data["resources"] == ",".join(f"{video.Video(bvid=b).get_aid()}:2" for b in [BV1, BV2])
        kit.states[BV1] = kit.states[BV2] = {20}
        return {}
    post = AsyncMock(side_effect=post)
    monkeypatch.setattr(kit.tool, "_post", post)
    result = await kit.tool.move(10, 20, [BV1, BV2], before_send=lambda: None)
    assert result == {"moved": [BV1, BV2], "verified": True, "units_spent": 2}
    assert (await kit.tool.move(10, 20, [BV1, BV2], before_send=lambda: None))["already_done"]
    assert post.await_count == 1


async def test_move_partial_completion_is_uncertain(kit, monkeypatch):
    async def post(*args):
        kit.states[BV1] = {20}
        return {}
    post = AsyncMock(side_effect=post)
    monkeypatch.setattr(kit.tool, "_post", post)
    with pytest.raises(AssistantError) as error:
        await kit.tool.move(10, 20, [BV1, BV2], before_send=lambda: None)
    assert error.value.code == "action_unconfirmed" and post.await_count == 1


async def test_missing_source_and_foreign_target_never_post(kit, monkeypatch):
    post = AsyncMock()
    monkeypatch.setattr(kit.tool, "_post", post)
    kit.states[BV1] = set()
    with pytest.raises(AssistantError):
        await kit.tool.move(10, 20, [BV1], before_send=lambda: None)
    with pytest.raises(AssistantError):
        await kit.tool.move(10, 999, [BV2], before_send=lambda: None)
    assert not post.called


async def test_post_guard_after_preparation_and_no_retry(kit, monkeypatch):
    sequence = []
    async def prepare(api):
        sequence.append("prepare")
        return {"method": "POST", "url": api.url, "data": api.data}
    async def request(**kwargs):
        sequence.append("post")
        raise TimeoutError("must-not-leak-cookie")
    monkeypatch.setattr(network.Api, "_prepare_request", prepare)
    monkeypatch.setattr(network, "get_client", lambda: SimpleNamespace(request=request))
    with pytest.raises(AssistantError) as error:
        await kit.tool._post("new", {}, lambda: sequence.append("guard"))
    assert sequence == ["prepare", "guard", "post"]
    assert error.value.code == "action_unconfirmed" and "cookie" not in str(error.value)
    sequence.clear()
    def stopped():
        raise AssistantError("stopped", "fixture")
    with pytest.raises(AssistantError) as error:
        await kit.tool._post("new", {}, stopped)
    assert sequence == ["prepare"] and error.value.code == "before_send_cancelled"


async def test_login_revision_change_blocks_read_result(kit, monkeypatch):
    version = ["v1"]
    def current(expected):
        if version[0] != expected:
            raise AssistantError("account_changed", "fixture")
    kit.adapter.credential_provider = SimpleNamespace(revision=lambda: version[0], _current=current)
    tool = FavoriteTools(kit.adapter, "123", lambda: None)
    async def listing(*args, **kwargs):
        version[0] = "v2"
        return {"count": 2, "list": kit.rows}
    monkeypatch.setattr(favorite_list, "get_video_favorite_list", listing)
    with pytest.raises(AssistantError) as error:
        await tool.list()
    assert error.value.code == "account_changed"


async def test_desktop_favorite_resolves_default_before_single_send(kit, monkeypatch):
    kit.adapter.credential_provider = SimpleNamespace(revision=lambda: "v1", _current=lambda expected: None)
    monkeypatch.setattr(video.Video, "has_favoured", AsyncMock(return_value=False))
    calls = []
    async def post(api, guard):
        guard()
        calls.append(api.data.copy())
        return {}
    monkeypatch.setattr("bili_assistant.publication.post_once", post)
    await kit.adapter.perform("favorite", {"bvid": BV1}, {"favorite_folder_id": 0}, lambda: None)
    assert len(calls) == 1 and calls[0]["add_media_ids"] == "10"


@pytest.fixture
def governed(kit, role, monkeypatch):
    kit.adapter.store = role.store
    role.adapter = kit.adapter
    monkeypatch.setattr(kit.adapter, "me", AsyncMock(return_value={"uid": "123"}))
    policy = role.store.read("policy")
    policy["data"]["actions"].update(favorite_create={"enabled": True, "daily_limit": 5},
                                     favorite_move={"enabled": True, "daily_limit": 50})
    role.store.write("policy", policy["data"], policy["version"], "fixture")
    return role


async def test_durable_quota_batch_accounting_and_changes(kit, governed, monkeypatch):
    from bili_assistant.action_quota import daily_usage

    engine = governed
    async def post(operation, data, guard):
        guard()
        if operation == "new":
            kit.rows.append(row(30, title=data["title"], attr=2 + data["privacy"]))
            return {"id": 30}
        kit.states[BV1] = kit.states[BV2] = {20}
        return {}
    monkeypatch.setattr(FavoriteTools, "_post", lambda self, *args: post(*args))
    created = engine.prepare("favorite_create", {"title": "新分类", "private": False})
    assert (await engine.execute(created["id"]))["status"] == "succeeded"
    action = engine.prepare("favorite_move", {"source": 10, "target": 20, "bvids": [BV1, BV2]})
    assert (await engine.execute(action["id"]))["status"] == "succeeded"
    assert (await engine.execute(action["id"]))["sent"] is False
    with engine.store.db() as db:
        used = daily_usage(db, "123", "2026-10-20")
    assert used == {"favorite_create": 1, "favorite_move": 2}
    assert engine.prepare("favorite_move", {"source": 10, "target": 20, "bvids": [BV2, BV1]})["duplicate"]
    policy = engine.store.read("policy")
    policy["data"]["actions"]["favorite_move"]["daily_limit"] = 1
    engine.store.write("policy", policy["data"], policy["version"], "fixture")
    more = engine.prepare("favorite_move", {"source": 20, "target": 10, "bvids": [BV1]})
    with pytest.raises(AssistantError) as error:
        await engine.execute(more["id"])
    assert error.value.code == "quota_exhausted"
    with engine.store.db() as db:
        assert daily_usage(db, "123", "2026-10-20") == used


async def test_unknown_overlap_survives_restart_and_readback_recovery(kit, governed, monkeypatch):
    from bili_assistant.favorite_actions import recover

    posts = []
    async def post(operation, data, guard):
        guard()
        posts.append(operation)
        raise AssistantError("action_unconfirmed", "模拟回执丢失")
    monkeypatch.setattr(FavoriteTools, "_post", lambda self, *args: post(*args))
    action = governed.prepare("favorite_move", {"source": 10, "target": 20, "bvids": [BV1]})
    assert (await governed.execute(action["id"]))["status"] == "uncertain"
    governed.stop()
    governed.start()
    # Changing the batch does not bypass a video with an unknown previous receipt.
    other = governed.prepare("favorite_move", {"source": 10, "target": 20, "bvids": [BV1, BV2]})
    with pytest.raises(AssistantError) as error:
        await governed.execute(other["id"])
    assert error.value.code == "favorite_unconfirmed" and posts == ["content_move"]
    tools = governed.adapter.favorite_tools(governed.role_run)
    assert await recover(governed, tools) == {"checked": 1, "verified": 0}
    kit.states[BV1] = {20}
    assert await recover(governed, tools) == {"checked": 1, "verified": 1}
    assert posts == ["content_move"]
    with governed.store.db() as db:
        saved = db.execute("SELECT status,units FROM actions WHERE id=?", (action["id"],)).fetchone()
    assert saved["status"] == "succeeded" and saved["units"] == 1


async def test_policy_change_at_dispatch_releases_reservation(kit, governed, monkeypatch):
    async def post(operation, data, guard):
        policy = governed.store.read("policy")
        policy["data"]["actions"]["favorite_create"]["enabled"] = False
        governed.store.write("policy", policy["data"], policy["version"], "fixture")
        guard()
        pytest.fail("disabled action reached submission")
    monkeypatch.setattr(FavoriteTools, "_post", lambda self, *args: post(*args))
    action = governed.prepare("favorite_create", {"title": "取消", "private": True})
    assert (await governed.execute(action["id"]))["status"] == "cancelled"
    with governed.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM actions WHERE status='succeeded'").fetchone()[0] == 0


async def test_typed_model_tools_read_create_and_batch_move(kit, governed, monkeypatch):
    from bili_assistant.favorite_workflow import step

    calls = []
    decisions = iter([
        {"tool": "contents", "arguments": {"folder_id": 10, "page": 1, "keyword": ""}},
        {"tool": "create", "arguments": {"title": "内容分类", "private": False}},
        {"tool": "move", "arguments": {"source": 10, "target": 30, "bvids": [BV1, BV2]}},
    ])
    async def model(prompt, context, schema, **kwargs):
        calls.append(copy.deepcopy(context))
        return {"call": next(decisions)}
    scheduler = SimpleNamespace(engine=governed, alive=governed.role_run, remaining_model_calls=lambda: 10, model=model)
    monkeypatch.setattr(favorite_list, "get_video_favorite_list_content", AsyncMock(return_value={
        "info": {"id": 10, "upper": {"mid": 123}}, "has_more": False,
        "medias": [{"bvid": b, "type": 2, "title": "中文分类", "intro": "简介", "attr": 0} for b in [BV1, BV2]]}))
    async def post(operation, data, guard):
        guard()
        if operation == "new":
            kit.rows.append(row(30, title=data["title"], attr=2))
            return {"id": 30}
        kit.states[BV1] = kit.states[BV2] = {30}
        return {}
    monkeypatch.setattr(FavoriteTools, "_post", lambda self, *args: post(*args))
    assert (await step(scheduler))["status"] == "succeeded"
    assert (await step(scheduler))["status"] == "succeeded"
    assert len(calls) == 3 and kit.states[BV1] == kit.states[BV2] == {30}
    assert calls[-1]["quota"] == {"favorite_create": 4, "favorite_move": 50}
    assert all(set(c) == {"directory", "content", "last_result", "quota"} for c in calls)


async def test_read_only_tools_do_not_call_model_or_write(kit, governed):
    from bili_assistant.favorite_workflow import step

    governed.stop()
    governed.start(read_only=True)
    model = AsyncMock()
    scheduler = SimpleNamespace(engine=governed, alive=governed.role_run, remaining_model_calls=lambda: 10, model=model)
    await step(scheduler)
    assert not model.called
    with pytest.raises(AssistantError) as error:
        governed.prepare("favorite_create", {"title": "不应创建", "private": False})
    assert error.value.code == "action_disabled"


async def test_recovery_remains_available_without_model_or_mutation_quota(kit, governed):
    from bili_assistant.workflow import BilibiliWorkflow

    action = governed.prepare("favorite_move", {"source": 10, "target": 20, "bvids": [BV1]})
    with governed.store.db() as db:
        db.execute("UPDATE actions SET status='dispatching',day='2026-10-20' WHERE id=?", (action["id"],))
    governed.stop()
    governed.start(read_only=True)
    kit.states[BV1] = {20}
    policy = governed.store.read("policy")
    for kind in ("favorite_create", "favorite_move"):
        policy["data"]["actions"][kind]["daily_limit"] = 0
    governed.store.write("policy", policy["data"], policy["version"], "fixture")
    model = AsyncMock()
    scheduler = SimpleNamespace(engine=governed, alive=governed.role_run, remaining_model_calls=lambda: 0, model=model)
    await BilibiliWorkflow(scheduler).delivery_recovery({})
    with governed.store.db() as db:
        assert db.execute("SELECT status FROM actions WHERE id=?", (action["id"],)).fetchone()["status"] == "succeeded"
    assert not model.called


async def test_new_tool_intent_can_organize_again_after_manual_move(kit, governed, monkeypatch):
    posts = []
    async def post(operation, data, guard):
        guard()
        posts.append(operation)
        kit.states[BV1] = {20}
        return {}
    monkeypatch.setattr(FavoriteTools, "_post", lambda self, *args: post(*args))
    for request_id in ("a" * 32, "b" * 32):
        kit.states[BV1] = {10}  # The owner later moved this video back manually.
        action = governed.prepare("favorite_move", {"source": 10, "target": 20, "bvids": [BV1], "request_id": request_id})
        assert (await governed.execute(action["id"]))["status"] == "succeeded"
        assert governed.prepare("favorite_move", {"source": 10, "target": 20, "bvids": [BV1], "request_id": request_id})["duplicate"]
    assert posts == ["content_move", "content_move"]


async def test_finished_unchanged_catalog_does_not_repeat_model_calls(kit, governed):
    from bili_assistant.favorite_workflow import step

    model = AsyncMock(return_value={"call": {"tool": "done", "arguments": {}}})
    scheduler = SimpleNamespace(engine=governed, alive=governed.role_run, remaining_model_calls=lambda: 10, model=model)
    await step(scheduler)
    await step(scheduler)
    assert model.await_count == 1
    kit.rows[0]["media_count"] += 1
    await step(scheduler)
    assert model.await_count == 2
