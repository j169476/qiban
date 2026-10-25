import json
from copy import deepcopy

import pytest
from test_cognition import LearningAdapter, configure, decision, material

from bili_assistant.api import call
from bili_assistant.bilibili import Bilibili
from bili_assistant.conversations import record_event
from bili_assistant.errors import AssistantError
from bili_assistant.rich_content import (
    comment_attachments,
    event_context,
    normalize_comment,
    pack_pages,
    private_content,
)
from bili_assistant.scheduler import Scheduler

IMAGE = "https://i0.hdslb.com/fixture.jpg"


def comment(rpid, root="100", parent="100"):
    return normalize_comment({
        "rpid": rpid, "root": root, "parent": parent, "mid": 789, "ctime": 1792476001,
        "member": {"uname": "原发言者"},
        "content": {"message": f"发言{rpid}", "pictures": [{"img_src": IMAGE}] if rpid == 100 else []},
    })


def inbox_event(role, kind="reply", attachments=()):
    data = {"id": "reply-999-101" if kind == "reply" else "dm-789-1", "kind": kind,
            "uid": "789", "display_name": "测试人", "received": role.clock().isoformat(),
            "oid": 999, "root": 100, "parent": 101, "text": "这张图是什么意思？", "attachments": list(attachments)}
    with role.store.db() as db:
        db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                   (data["id"], "123", "789", kind, data["received"], json.dumps(data)))
    record_event(role.store, "123", data)
    return data


class CommentAdapter(LearningAdapter):
    def __init__(self, total=25):
        super().__init__()
        self.total = total
        self.pages = []

    async def comment_page(self, oid, root, page):
        self.pages.append(page)
        return {"total": self.total, "page_size": 20, "root_comment": comment(100, "", ""),
                "replies": [comment(101 + i) for i in range((page - 1) * 20, min(page * 20, self.total))]}


def test_card_and_picture_parsers_keep_actual_sources_and_drop_unsafe_or_unknown():
    text = private_content({"msg_type": 1, "content": json.dumps({"content": "看 BV1xx411c7mD"})})
    assert text["bvid"] == "BV1xx411c7mD"
    picture = private_content({"msg_type": 2, "content": json.dumps({"url": IMAGE, "original": True})})
    assert picture["attachments"][0]["url"] == IMAGE
    shared = private_content({"msg_type": 7, "content": json.dumps({"bvid": "BV1xx411c7mD", "title": "参考视频"})})
    assert shared["bvid"] == "BV1xx411c7mD" and shared["text"] == "[分享视频] 参考视频"
    by_aid = private_content({"msg_type": 11, "content": json.dumps({"id": 170001, "title": "分享"})})
    assert by_aid["attachments"][0]["kind"] == "video_card"
    assert private_content({"msg_type": 2, "content": json.dumps({"url": "http://127.0.0.1/secrets"})}) is None
    assert private_content({"msg_type": 99, "content": "{}"}) is None
    assert private_content({"msg_type": 1, "content": "["}) is None
    attachments = comment_attachments({
        "pictures": [{"img_src": IMAGE}, {"img_src": "file:///secret"}],
        "jump_url": {
            "https://www.bilibili.com/video/BV1xx411c7mD": {"title": "跳转视频"},
            "https://bilibili.com.evil.test/video/BV1xx411c7mD": {"title": "伪造域名"},
            "https://[bad": {"title": "坏链接"},
        },
    })
    assert [a["kind"] for a in attachments] == ["image", "video_card"]


@pytest.mark.asyncio
async def test_comment_pagination_can_continue_and_marks_limits_and_manual_overrides(role, monkeypatch):
    role.adapter = CommentAdapter(25)
    configure(role, comment_context_pages=1, visuals_enabled=False)
    event = inbox_event(role)
    first = await event_context(role, event["id"])
    assert not first["comment_context"]["complete"] and first["comment_context"]["next_page"] == 2
    assert len(first["comment_context"]["comments"]) == 21
    second = await event_context(role, event["id"], page=2)
    assert second["comment_context"]["complete"] and len(second["comment_context"]["comments"]) == 26
    assert second["comment_context"]["next_page"] is None
    pages = role.store.records("comment_pages")[0]
    assert not pack_pages(pages, 1000)["complete"]
    assert pack_pages(pages, 1000)["context_truncated"]
    altered = next(p for p in pages if p["data"]["page"] == 1)
    altered["data"]["root_comment"]["text"] = "我的本地修正"
    role.store.write("comment_pages", altered["data"], altered["version"], "user", altered["data"]["id"])
    reread = await event_context(role, event["id"])
    assert reread["comment_context"]["comments"][0]["text"] == "我的本地修正"
    assert reread["comment_context"]["manual_override"] and not reread["comment_context"]["complete"]
    # Context did not turn the 26 historical comments into inbox events.
    with role.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert len(role.store.records("conversation_turns")[0]) == 1
    assert not role.store.records("interest_signals")[0]


@pytest.mark.asyncio
async def test_unknown_event_and_admin_cannot_read_private_context(role, admin):
    with pytest.raises(AssistantError, match="实际收到"):
        await event_context(role, "unknown")
    event = inbox_event(role)
    with pytest.raises(AssistantError, match="role"):
        await call(admin, "bili_event_context", {"event_id": event["id"]})


@pytest.mark.asyncio
async def test_long_comment_thread_prioritizes_recent_trigger_then_can_fill_missing_pages(role):
    role.adapter = CommentAdapter(85)
    configure(role, comment_context_pages=2, visuals_enabled=False, comment_context_chars=40000)
    event = inbox_event(role)
    event["parent"] = 185
    with role.store.db() as db:
        db.execute("UPDATE events SET body=? WHERE account=? AND id=?", (json.dumps(event), "123", event["id"]))
    first = await event_context(role, event["id"])
    assert role.adapter.pages == [1, 5]
    assert any(c["rpid"] == "185" for c in first["comment_context"]["comments"])
    assert not first["comment_context"]["complete"] and first["comment_context"]["next_page"] == 2
    await event_context(role, event["id"], page=2)
    finished = await event_context(role, event["id"], page=4)
    assert finished["comment_context"]["complete"] and len(finished["comment_context"]["comments"]) == 86


@pytest.mark.asyncio
async def test_actual_image_path_reaches_codex_decision_and_preserves_video_frames(role, monkeypatch):
    import bili_assistant.rich_content as rich

    role.adapter = CommentAdapter(0)
    configure(role, visuals_max_images=3)
    event = inbox_event(role, attachments=[{"kind": "image", "url": IMAGE, "title": "", "bvid": ""}] * 3)
    folder = role.store.root / "cache" / "media"
    folder.mkdir(parents=True)
    path = folder / "fixture.jpg"
    path.write_bytes(b"\xff\xd8\xfffixture")
    async def download(*args):
        return str(path)
    monkeypatch.setattr(rich, "download_image", download)
    scheduler = Scheduler(role, role.run()["generation"])
    async def model(prompt, payload, schema, images=()):
        assert len(images) == 4
        assert len(payload["material"]["events"][0]["images"]) == 1
        assert payload["material"]["events"][0]["images_omitted"] == 3
        assert len(payload["material"]["video"]["images"]) == 3
        assert payload["material"]["image_order"][0]["event_id"] == event["id"]
        return decision()
    monkeypatch.setattr(scheduler, "model", model)
    video = material(kind="frames")
    video["images"] = [{"kind": "preview_sheet", "path": str(path), "timestamps_seconds": [i]} for i in range(3)]
    await scheduler.decide({"events": [event], "video": video})
    assert role.store.records("conversation_turns")[0][0]["data"]["attachments"][0]["url"] == IMAGE


@pytest.mark.asyncio
async def test_comment_page_adapter_validates_root_and_preserves_reply_targets(store, monkeypatch):
    from bilibili_api import comment as sdk

    import bili_assistant.credentials as credentials

    monkeypatch.setattr(credentials, "load", lambda *args, **kwargs: object())
    raw = {"page": {"num": 1, "size": 20, "count": 1},
           "root": {"rpid": 100, "mid": 789, "content": {"message": "根评论"}},
           "replies": [{"rpid": 101, "mid": 456, "root": 100, "parent": 100, "content": {"message": "子回复"}}]}
    class Comment:
        def __init__(self, oid, kind, root, credential):
            assert oid == 999 and root == 100

        async def get_sub_comments(self, page_index, page_size):
            assert page_index == 1 and page_size == 20
            return deepcopy(raw)
    monkeypatch.setattr(sdk, "Comment", Comment)
    page = await Bilibili(store).comment_page(999, 100)
    assert page["root_comment"]["text"] == "根评论"
    assert page["replies"][0]["parent"] == "100" and page["replies"][0]["uid"] == "456"
    raw["root"]["rpid"] = 999
    with pytest.raises(AssistantError, match="不同的根"):
        await Bilibili(store).comment_page(999, 100)


@pytest.mark.asyncio
async def test_private_inbox_accepts_cards_and_images_but_never_own_or_old_messages(store, monkeypatch):
    from bilibili_api import session

    import bili_assistant.credentials as credentials

    monkeypatch.setattr(credentials, "load", lambda *args, **kwargs: object())
    async def empty(*args):
        return {"items": []}
    async def sessions(*args, **kwargs):
        return {"session_list": [{"talker_id": 789, "session_ts": 1792476001 * 1000000}]}
    async def messages(*args):
        return {"messages": [
            {"sender_uid": sender, "msg_seqno": i, "timestamp": stamp, "msg_type": kind, "content": json.dumps(body)}
            for i, (sender, stamp, kind, body) in enumerate([
                (789, 1792476001, 2, {"url": IMAGE}),
                (789, 1792476001, 7, {"bvid": "BV1xx411c7mD", "title": "来看"}),
                (123, 1792476001, 1, {"content": "我自己发的"}),
                (789, 1, 1, {"content": "旧消息"}),
            ])
        ]}
    monkeypatch.setattr(session, "get_replies", empty)
    monkeypatch.setattr(session, "get_at", empty)
    monkeypatch.setattr(session, "get_sessions", sessions)
    monkeypatch.setattr(session, "fetch_session_msgs", messages)
    events = await Bilibili(store).inbox("2026-10-20T00:00:00+00:00")
    assert len(events) == 2
    assert events[0]["attachments"][0]["kind"] == "image"
    assert events[1]["bvid"] == "BV1xx411c7mD"
