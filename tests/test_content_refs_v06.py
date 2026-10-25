"""Bounded site references and typed comment resources, entirely fake platform I/O."""

import json
from copy import deepcopy

import httpx
import pytest

from bili_assistant.bilibili import Bilibili
from bili_assistant.content_refs import event_video, parse_video, references, resolve_short
from bili_assistant.conversations import event_thread
from bili_assistant.errors import AssistantError
from bili_assistant.rich_content import event_context, private_content

BV = "BV1xx411c7mD"


@pytest.mark.parametrize("value,page", [(BV, 0), (f"https://www.bilibili.com/video/{BV}?p=3", 2),
                                      (f"//www.bilibili.com/video/{BV}/?p=2&amp;spm=1", 1),
                                      ("https://www.bilibili.com/video/av170001?p=4", 3)])
def test_video_reference_preserves_page(value, page):
    assert parse_video(value)["page_index"] == page


@pytest.mark.parametrize("value", [f"https://bilibili.com.evil.test/video/{BV}",
    f"https://user@www.bilibili.com/video/{BV}", f"https://www.bilibili.com:99/video/{BV}",
    f"https://www.bilibili.com/video/{BV}?p=0", f"https://www.bilibili.com/video/{BV}?p=1&p=2",
    f"https://www.bilibili.com/video/{BV}?p=hello", f"https://example.com/{BV}", "file:///secret"])
def test_untrusted_or_ambiguous_reference_is_not_a_video(value):
    assert parse_video(value) is None
    assert event_video({"text": value}) is None


def test_text_and_share_cards_keep_original_page_without_fetching():
    text = f"来看 https://www.bilibili.com/video/{BV}?p=3。"
    assert event_video({"text": text})["page_index"] == 2
    assert references(f"假的 https://example.com/{BV} 真的 {BV}") == [BV]
    msg = private_content({"msg_type": 1, "content": {"content": text}})
    assert msg["bvid"] == BV and msg["page_index"] == 2
    card = private_content({"msg_type": 7, "content": {"bvid": BV, "title": "分P",
                                                     "url": f"https://www.bilibili.com/video/{BV}?p=4"}})
    assert card["page_index"] == 3 and card["attachments"][0]["url"].endswith("?p=4")
    assert event_video({**card, "text": f"[分享视频] {BV}"})["page_index"] == 3
    assert references("看 b23.tv/Abc123") == ["b23.tv/Abc123"]


def mock_http(monkeypatch, handler):
    import bili_assistant.content_refs as module

    client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.asyncio
async def test_short_link_checks_every_hop_and_never_fetches_final_video(monkeypatch):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        assert request.url.host == "b23.tv"
        target = "/second" if request.url.path == "/first" else f"https://www.bilibili.com/video/{BV}?p=3"
        return httpx.Response(302, headers={"Location": target})

    mock_http(monkeypatch, handler)
    result = await resolve_short("https://b23.tv/first")
    assert requested == ["https://b23.tv/first", "https://b23.tv/second"]
    assert result["page_index"] == 2 and result["source_url"] == "https://b23.tv/first"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["https://evil.test/path", "http://127.0.0.1/secret",
    f"https://b23.tv.evil.test/{BV}", f"https://www.bilibili.com@evil.test/video/{BV}",
    "https://b23.tv:8443/a", "https://www.bilibili.com/redirect?url=https://evil.test"])
async def test_short_link_never_contacts_disallowed_redirect(monkeypatch, target):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": target})

    mock_http(monkeypatch, handler)
    with pytest.raises(AssistantError) as error:
        await resolve_short("https://b23.tv/one")
    assert error.value.code == "short_link_scope"
    assert requested == ["https://b23.tv/one"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,code", [("loop", "short_link_loop"), ("hops", "short_link_hops"),
                                        ("timeout", "short_link_unavailable")])
async def test_short_link_failures_are_bounded(monkeypatch, scenario, code):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if scenario == "timeout":
            raise httpx.ReadTimeout("fake timeout", request=request)
        target = "https://b23.tv/one" if scenario == "loop" else f"https://b23.tv/next{len(requested)}"
        return httpx.Response(302, headers={"Location": target})

    mock_http(monkeypatch, handler)
    with pytest.raises(AssistantError) as error:
        await resolve_short("https://b23.tv/one")
    assert error.value.code == code and len(requested) <= 3


def record_invitation(role, text="https://b23.tv/one", uid="789"):
    settings = role.store.read("companion")
    settings["data"]["guide_uid"] = "789"
    role.store.write("companion", settings["data"], settings["version"], "test")
    event = {"id": "dm-guide-1", "kind": "dm_reply", "uid": uid,
             "received": role.clock().isoformat(), "text": text, "attachments": []}
    with role.store.db() as db:
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (
            event["id"], "123", uid, "dm_reply", event["received"], json.dumps(event)))
    return event


@pytest.mark.asyncio
async def test_guide_short_link_is_derived_cached_and_invalidated_by_source_edits(role, monkeypatch):
    original = record_invitation(role)
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": f"https://www.bilibili.com/video/{BV}?p=2"})

    mock_http(monkeypatch, handler)
    result = await role.resolve_invitation({**original, "is_guide": True})
    assert result["bvid"] == BV and result["page_index"] == 1
    assert len(requested) == 1
    assert (await role.resolve_invitation(original))["page_index"] == 1
    assert len(requested) == 1
    with role.store.db() as db:
        stored = json.loads(db.execute("SELECT body FROM events WHERE id=?", (original["id"],)).fetchone()[0])
        assert stored == original
        changed = {**original, "text": f"https://www.bilibili.com/video/{BV}?p=4"}
        db.execute("UPDATE events SET body=? WHERE id=?", (json.dumps(changed), original["id"]))
    assert (await role.resolve_invitation(changed))["page_index"] == 3
    assert not role.store.records("identity_links")[0]


@pytest.mark.asyncio
async def test_non_guide_or_unobserved_reference_does_not_resolve(role, monkeypatch):
    original = record_invitation(role, uid="456")

    def unexpected(request):
        pytest.fail("non-guide link must not cause HTTP")

    mock_http(monkeypatch, unexpected)
    assert await role.resolve_invitation(original) == original
    with pytest.raises(AssistantError):
        await role.resolve_invitation({**original, "id": "not-received"})


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "original"])
async def test_changed_run_or_invitation_cancels_short_link_result(role, monkeypatch, change):
    original = record_invitation(role)

    def handler(request):
        if change == "pause":
            (role.store.runtime / f"stop-{role.run()['generation']}").write_text("pause", encoding="utf-8")
        else:
            with role.store.db() as db:
                db.execute("UPDATE events SET body=? WHERE id=?", (json.dumps({**original, "text": "已撤回"}), original["id"]))
        return httpx.Response(302, headers={"Location": f"https://www.bilibili.com/video/{BV}?p=2"})

    mock_http(monkeypatch, handler)
    with pytest.raises(AssistantError):
        await role.resolve_invitation(original)
    assert role.store.state("invitation-reference:123:" + original["id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_comment_explicit_short_invitation_never_falls_back_to_hosting_video(role, monkeypatch, unavailable):
    parent_bvid = "BV1RT4y1h7YY"
    original = record_invitation(role, text="请看 https://b23.tv/one")
    original.update(kind="reply", bvid=parent_bvid, page_index=0, oid=999, root=10, parent=11)
    with role.store.db() as db:
        db.execute("UPDATE events SET kind='reply',body=? WHERE id=?", (json.dumps(original), original["id"]))
    assert event_video(original) is None
    assert event_video({**original, "text": "请看当前视频"})["bvid"] == parent_bvid

    def handler(request):
        if unavailable:
            raise httpx.ReadTimeout("fake timeout", request=request)
        return httpx.Response(302, headers={"Location": f"https://www.bilibili.com/video/{BV}?p=2"})

    mock_http(monkeypatch, handler)
    result = await role.resolve_invitation(original)
    if unavailable:
        assert result["bvid"] == "" and result["video_reference_error"] == "short_link_unavailable"
        assert role.store.state("invitation-reference:123:" + original["id"]) is None
    else:
        assert result["bvid"] == BV and result["page_index"] == 1
        assert (await role.resolve_invitation(original))["bvid"] == BV
    with role.store.db() as db:
        assert json.loads(db.execute("SELECT body FROM events WHERE id=?", (original["id"],)).fetchone()[0]) == original


@pytest.mark.asyncio
async def test_notifications_route_only_explicit_supported_resource_types(store, monkeypatch):
    from bilibili_api import session

    import bili_assistant.credentials as credentials

    monkeypatch.setattr(credentials, "load", lambda *a, **kw: object())

    async def replies(_credential):
        return {"items": [{"reply_time": 1792476001, "user": {"mid": 789}, "item": {
            "subject_id": 999, "source_id": 111, "root_id": 111, "type": kind,
            "business_id": resource, "source_content": "测试", "business": "动态",
        }} for resource, kind in [(1, "reply"), (11, "reply"), (17, "dynamic"), (12, "reply"), (99, "dynamic"), (17, "unknown")]]}

    async def empty(*args, **kwargs):
        return {}

    monkeypatch.setattr(session, "get_replies", replies)
    monkeypatch.setattr(session, "get_at", empty)
    monkeypatch.setattr(session, "get_sessions", empty)
    events = await Bilibili(store).inbox("2026-10-20T00:00:00+00:00")
    assert [e["resource_type"] for e in events] == [1, 11, 17]
    assert len({e["id"] for e in events}) == len({event_thread(e) for e in events}) == 3
    assert events[0]["bvid"] and not any(e["bvid"] for e in events[1:])


@pytest.mark.asyncio
async def test_comment_pages_keep_dynamic_and_video_with_same_ids_separate(role):
    settings = role.store.read("companion")
    settings["data"]["visuals_enabled"] = False
    role.store.write("companion", settings["data"], settings["version"], "test")
    calls = []

    class Adapter:
        async def comment_page(self, oid, root, page, *, resource_type=1):
            calls.append(resource_type)
            return {"total": 0, "page_size": 20, "replies": [], "root_comment": {
                "rpid": "111", "uid": "789", "display_name": "用户", "root": "", "parent": "",
                "text": f"资源{resource_type}", "received": None, "attachments": []}}

    role.adapter = Adapter()
    for resource in (1, 11, 17):
        event = {"id": f"reply-type-{resource}", "kind": "reply", "uid": "789", "oid": 999,
                 "root": 111, "parent": 111, "resource_type": resource,
                 "received": role.clock().isoformat(), "text": "测试", "attachments": []}
        with role.store.db() as db:
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (
                event["id"], "123", "789", "reply", event["received"], json.dumps(event)))
        context = await event_context(role, event["id"])
        assert context["comment_context"]["comments"][0]["text"] == f"资源{resource}"
    assert calls == [1, 11, 17]
    assert len(role.store.records("comment_pages")[0]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", [11, 17])
async def test_dynamic_reply_and_like_transport_preserve_verified_resource_type(store, monkeypatch, resource):
    from bilibili_api import Credential

    import bili_assistant.credentials as credentials
    import bili_assistant.publication as publication

    monkeypatch.setattr(credentials, "load", lambda *a, **kw: Credential(sessdata="fake", bili_jct="fake"))
    sent = []

    async def post(api, guard):
        guard()
        sent.append(deepcopy(api.data))
        return {"rpid": "222"}

    monkeypatch.setattr(publication, "post_once", post)
    event = {"oid": 999, "root": 111, "parent": 222, "resource_type": resource}
    await Bilibili(store).perform("reply", {"text": "动态回复", "_event": event}, {}, lambda: None)
    await Bilibili(store).perform("comment_like", {"_event": event}, {}, lambda: None)
    assert len(sent) == 2 and all(data["type"] == resource and data["oid"] == 999 for data in sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("native_core", [False, True])
async def test_same_video_different_pages_are_read_and_consumed_in_separate_cycles(role, monkeypatch, native_core):
    from test_cognition import LearningAdapter, configure, decision, material, signal

    from bili_assistant.core_binding import companion
    from bili_assistant.interests import interest_profile
    from bili_assistant.scheduler import Scheduler

    configure(role, guide_uid="789", daily_reflection=False, memory_maintenance=False)
    invitations = [{"id": f"dm-guide-{index}", "kind": "dm_reply", "uid": "789", "attachments": [],
                    "received": role.clock().isoformat(), "text": text}
                   for index, text in enumerate(["https://b23.tv/first", f"https://www.bilibili.com/video/{BV}?p=4"], 1)]

    class Adapter(LearningAdapter):
        async def video_context(self, bvid, page_index=0):
            self.read_videos.append((bvid, page_index))
            result = material(bvid)
            result.update(page_index=page_index, cid=str(page_index + 1),
                          title=f"分P {page_index + 1}", url=f"https://www.bilibili.com/video/{bvid}?p={page_index + 1}")
            return result

    adapter = Adapter(events=invitations)
    role.adapter = adapter
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": f"https://www.bilibili.com/video/{BV}?p=2"})

    mock_http(monkeypatch, handler)
    discussed = []

    async def model(instructions, context, schema, images=(), **options):
        events = context["material"]["events"]
        assert len(events) == 1
        event = events[0]
        assert context["material"]["video"]["page_index"] == event["page_index"]
        discussed.append((event["id"], event["page_index"]))
        return decision(native_request=None, video_gap=None, interest_signals=[signal(event["id"])])

    if native_core:
        role.stop(paused=True)

        class Model:
            generate = staticmethod(model)

        controller = companion(role, Model())
        controller.start()
    else:
        controller = Scheduler(role, role.run()["generation"])
        monkeypatch.setattr(controller, "model", model)
    await controller.cycle()
    assert role.store.state("processed:123:dm-guide-1")
    assert not role.store.state("processed:123:dm-guide-2")
    adapter.events = []
    await controller.cycle()
    assert role.store.state("processed:123:dm-guide-2")
    assert discussed == [("dm-guide-1", 1), ("dm-guide-2", 3)]
    assert adapter.read_videos == [(BV, 1), (BV, 3)]
    assert requested == ["https://b23.tv/first"]
    assert len(role.store.records("interest_signals")[0]) == 2
    assert interest_profile(role.store)["topics"][0]["supporting_videos"] == 1
    assert not adapter.sent
    with role.store.db() as db:
        raw = [json.loads(row[0]) for row in db.execute("SELECT body FROM events ORDER BY id")]
    assert raw == invitations


@pytest.mark.asyncio
@pytest.mark.parametrize("native_core,resource", [(False, 11), (True, 17)])
async def test_typed_dynamic_reply_runs_through_controller_and_archives_separate_thread(role, monkeypatch, native_core, resource):
    from bilibili_api import Credential
    from test_cognition import configure, decision

    import bili_assistant.credentials as credentials
    import bili_assistant.publication as publication
    from bili_assistant.core_binding import companion
    from bili_assistant.scheduler import Scheduler

    configure(role, daily_reflection=False, memory_maintenance=False, visuals_enabled=False)
    event = {"id": f"reply-{resource}-999-222", "kind": "reply", "uid": "789", "oid": 999,
             "root": 111, "parent": 222, "resource_type": resource, "bvid": "",
             "received": role.clock().isoformat(), "text": "你怎么看这条动态？", "attachments": []}
    adapter = Bilibili(role.store)
    sent = []

    async def me():
        return {"uid": "123"}

    async def inbox(started_at):
        return [event]

    async def feed(**kwargs):
        return []

    async def comments(oid, root, page, *, resource_type):
        assert resource_type == resource
        return {"total": 0, "page_size": 20, "replies": [], "root_comment": {
            "rpid": "111", "uid": "789", "display_name": "用户", "root": "", "parent": "",
            "text": "动态根评论", "received": None, "attachments": []}}

    async def post(api, guard):
        guard()
        sent.append(deepcopy(api.data))
        return {"rpid": "333"}

    monkeypatch.setattr(credentials, "load", lambda *a, **kw: Credential(sessdata="fake", bili_jct="fake"))
    monkeypatch.setattr(publication, "post_once", post)
    monkeypatch.setattr(adapter, "me", me)
    monkeypatch.setattr(adapter, "inbox", inbox)
    monkeypatch.setattr(adapter, "feed", feed)
    monkeypatch.setattr(adapter, "comment_page", comments)
    role.adapter = adapter

    async def model(instructions, context, schema, images=(), **options):
        assert context["material"]["events"][0]["resource_type"] == resource
        return decision(native_request=None, video_gap=None, action={"kind": "reply", "payload": {
            "event_id": event["id"], "text": "我理解这条动态中的问题。", "bvid": "", "uid": "", "units": 1}})

    if native_core:
        role.stop(paused=True)

        class Model:
            generate = staticmethod(model)

        controller = companion(role, Model())
        controller.start()
    else:
        controller = Scheduler(role, role.run()["generation"])
        monkeypatch.setattr(controller, "model", model)
    await controller.cycle()
    assert len(sent) == 1 and sent[0]["type"] == resource
    assert (sent[0]["oid"], sent[0]["root"], sent[0]["parent"]) == (999, 111, 222)
    turns = role.store.records("conversation_turns")[0]
    assert {row["data"]["thread_key"] for row in turns} == {f"{resource}:999:111"}
    assert any(row["data"]["speaker"] == "assistant" and row["data"]["status"] == "sent" for row in turns)
