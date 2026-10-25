"""Exercise adapter contract/edge paths without HTTP or real credentials."""

import pytest

from bili_assistant.bilibili import Bilibili
from bili_assistant.errors import AssistantError


@pytest.mark.asyncio
async def test_follow_reads_actual_relationship(store, monkeypatch):
    from bilibili_api import user

    import bili_assistant.credentials as credentials

    monkeypatch.setattr(credentials, "load", lambda *args, **kwargs: object())
    calls = []

    class User:
        def __init__(self, *args, **kwargs):
            pass

        async def get_relation(self):
            return {"relation": {"attribute": 0}, "be_relation": {"attribute": 2}}

        async def modify_relation(self, relation):
            calls.append(relation)

    monkeypatch.setattr(user, "User", User)
    result = await Bilibili(store).perform("follow", {"uid": "777"}, {}, lambda: None)
    assert result == {"already_done": True, "units_spent": 0}
    assert not calls


@pytest.mark.asyncio
async def test_last_guard_runs_after_preflight(role, monkeypatch):
    from bilibili_api import video

    import bili_assistant.credentials as credentials

    monkeypatch.setattr(credentials, "load", lambda *args, **kwargs: object())
    sent = []

    class Video:
        def __init__(self, *args, **kwargs):
            pass

        async def has_liked(self):
            run = role.store.state("run")
            (role.store.runtime / f"stop-{run['generation']}").write_text("archive")
            return False

        async def like(self):
            sent.append("post")

    monkeypatch.setattr(video, "Video", Video)
    adapter = Bilibili(role.store)

    async def post(kind, item, credential, data, before_send):
        before_send()
        sent.append("post")

    monkeypatch.setattr(adapter, "_video_post", post)

    async def me():
        return {"uid": "123"}

    monkeypatch.setattr(adapter, "me", me)
    role.adapter = adapter
    role.store.set_state("context:BV1xx411c7mD", {"owner_uid": "888", "coverage": {"kind": "metadata_only"}})
    action = role.prepare("like", {"bvid": "BV1xx411c7mD"})
    assert (await role.execute(action["id"]))["status"] == "cancelled"
    assert not sent


@pytest.mark.asyncio
async def test_coin_adds_only_missing_amount(store, monkeypatch):
    from bilibili_api import video

    import bili_assistant.credentials as credentials

    monkeypatch.setattr(credentials, "load", lambda *args, **kwargs: object())
    calls = []
    readings = iter([1, 2])

    class Video:
        def __init__(self, *args, **kwargs):
            pass

        async def get_pay_coins(self):
            return next(readings)

    monkeypatch.setattr(video, "Video", Video)
    async def post(self, kind, item, credential, data, before_send):
        assert data["select_like"] == 0 and "like" not in data
        before_send()
        calls.append(data["multiply"])
        return {}

    monkeypatch.setattr(Bilibili, "_video_post", post)
    await Bilibili(store).perform("coin", {"bvid": "BV1xx411c7mD", "units": 2}, {}, lambda: None)
    assert calls == [1]


def test_knowledge_requires_observed_coverage(role):
    with pytest.raises(AssistantError, match="实际读取"):
        role.remember("knowledge", {"coverage": {"bvid": "BV1xx411c7mD"}})


@pytest.mark.asyncio
async def test_inbox_maps_video_replies_and_excludes_old_messages(store, monkeypatch):
    from bilibili_api import session

    import bili_assistant.credentials as credentials

    monkeypatch.setattr(credentials, "load", lambda *args, **kwargs: object())

    async def replies(credential):
        return {
            "items": [
                {
                    "id": 55,
                    "reply_time": 1792476001,
                    "user": {"mid": 789},
                    "item": {
                        "type": "reply",
                        "business_id": 1,
                        "subject_id": 999,
                        "root_id": 111,
                        "source_id": 222,
                        "source_content": "一个真实回复",
                    },
                },
                {
                    "id": 56,
                    "reply_time": 1,
                    "user": {"mid": 789},
                    "item": {"type": "reply", "business_id": 1, "subject_id": 999, "source_id": 333},
                },
            ]
        }

    async def sessions(credential, session_type):
        return {"session_list": []}

    async def mentions(credential):
        item = (await replies(credential))["items"][0]
        return {
            "items": [
                {**item, "id": 999, "at_time": item["reply_time"]},
                {
                    **item,
                    "id": 1000,
                    "at_time": item["reply_time"],
                    "item": {**item["item"], "root_id": 0, "source_id": 444},
                },
                {**item, "id": 1001, "at_time": 1},
                {
                    **item,
                    "id": 1002,
                    "at_time": item["reply_time"],
                    "item": {**item["item"], "business_id": 12},
                },
            ]
        }

    monkeypatch.setattr(session, "get_replies", replies)
    monkeypatch.setattr(session, "get_at", mentions)
    monkeypatch.setattr(session, "get_sessions", sessions)
    events = await Bilibili(store).inbox("2026-10-20T00:00:00+00:00")
    assert len(events) == 2
    assert events[0]["oid"] == 999 and events[0]["parent"] == 222 and events[0]["root"] == 111
    assert events[0]["id"] == "reply-999-222"
    assert events[1]["root"] == events[1]["parent"] == 444
