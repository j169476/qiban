"""Synthetic material enters the same event store, review worker and recipient projections as desktop."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from companion_core import events, privacy, privacy_review
from companion_core.contracts import ActorRef, Event
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from qiban.desktop_scheduler import DesktopScheduler
from qiban.desktop_tasks import TaskBarrier
from tests_postgres.test_qq_module import env as env
from tests_postgres.test_qq_module import handle, incoming

pytestmark = pytest.mark.asyncio


def source(text, who, room, seq, time, visibility="group", addressed=()):
    return Event(
        "qq",
        "10001",
        str(seq),
        ActorRef("qq", who, "10001"),
        visibility + ":" + room,
        time.isoformat(),
        text,
        visibility,
        metadata={
            "qq": {"target_id": room, "addressed_members": list(addressed), "identity_quality": "verified"}
        },
    )


def answer(quote, value, category, level, evidence, subject="none", **extra):
    return (
        dict(
            quote=quote,
            value=value,
            category=category,
            level=level,
            evidence=evidence,
            subject=subject,
            until=None,
        )
        | extra
    )


async def reviewed(env, ev, facts):
    ref = events.ingest(env.store, ev)
    key = "event/" + ref["id"]

    async def classify(prompt, material, schema, *args, **kwargs):
        assert material["current"] == privacy.mask(ev.text)
        assert "sk-testCredential12345678" not in str(material)
        return {"facts": facts}

    env.model.generate = classify
    await privacy_review.tick(env.core)
    with env.store.db() as db:
        assert db.execute("SELECT complete FROM privacy_jobs WHERE resource=?", (key,)).fetchone()[0]
    return key


async def test_invited_house_shop_itinerary_credential_and_later_correction(env):
    now = datetime.now(timezone.utc)
    a, b, c = "10002", "10004", "10005"
    room = "10003"

    def dm(who, topic):
        return source(topic, who, who, topic + who, now, "private")

    ab = ActorRef("qq", b, "10001")
    ev = source("春和棋馆在春和路18号。周五我要去那里。密码=sk-testCredential12345678", a, room, 1, now)
    venue = answer("春和棋馆在春和路18号", "春和路18号", "venue", "social", "venue")
    itinerary = answer("周五我要去那里", "周五", "itinerary", "private", "self", "speaker")
    too_broad = answer("春和棋馆在春和路18号。周五我要去那里", "", "ordinary", "social", "general")
    key = await reviewed(env, ev, [venue, itinerary, too_broad])
    values, pins = privacy.project(env.store, dm(b, "棋馆在哪里"), [key])
    assert [r["text"] for r in values] == ["春和路18号"]
    public = Event(
        "bilibili",
        "123",
        "p",
        ActorRef("bilibili", "333"),
        "comment:x",
        now.isoformat(),
        "棋馆在哪里",
        "public",
    )
    assert privacy.project(env.store, public, [key]) == ([], [])
    home = source("纠正一下，春和路18号其实是我家", a, room, 2, now + timedelta(seconds=1))
    await reviewed(env, home, [answer(home.text, "春和路18号", "home", "private", "self", "speaker")])
    assert not privacy.project(env.store, dm(b, "棋馆在哪里"), [key])[0]
    from companion_core.errors import AssistantError

    with pytest.raises(AssistantError):
        privacy.check_projection(env.store, dm(b, "棋馆在哪里"), pins)
    for seq, person in enumerate((b, c), 3):
        await reviewed(env, source("你好", person, room, seq, now), [])
    invitation = "周六桌游邀请你来春和路18号"
    ev = source(invitation, a, room, 7, now, addressed=[ab.key])
    fact = answer(
        invitation,
        "春和路18号",
        "meeting",
        "circle",
        "invitation",
        occasion="周六桌游",
        until=(now + timedelta(days=1)).isoformat(),
    )
    invited = await reviewed(env, ev, [fact])
    assert privacy.project(env.store, dm(b, "周六桌游在哪"), [invited])[0]
    for target in (
        dm(c, "周六桌游在哪"),
        dm(b, "下周围棋赛在哪"),
        source("周六桌游在哪", c, room, 9, now),
        public,
    ):
        assert not privacy.project(env.store, target, [invited])[0]
    expired = source(invitation, a, room, 8, now, addressed=[ab.key])
    key = await reviewed(env, expired, [{**fact, "until": (now - timedelta(seconds=1)).isoformat()}])
    assert not privacy.project(env.store, dm(b, "周六桌游在哪"), [key])[0]


async def test_natural_names_across_distinct_circles_and_forwarding(env):
    now = datetime.now(timezone.utc)
    auth = ReplyAuthorizations(env.store)
    auth.update(
        ReplyScope("qq", "10001", "group", "20003"), enabled=True, expected_version=None, actor="fixture"
    )
    outsider = source("你好", "10009", "10009", 900, now, "private")
    for ix, room in enumerate(["10003", "20003"]):
        base = now + timedelta(days=ix)
        intro = source("平时叫我小青", "10002", room, 20 + ix, base)
        key = await reviewed(env, intro, [answer(intro.text, "小青", "name", "private", "self", "speaker")])
        copy = source("转发：平时叫我小青", "10008", room, 40 + ix, base)
        await reviewed(env, copy, [answer("平时叫我小青", "小青", "name", "social", "self", "speaker")])
        assert not privacy.project(env.store, outsider, [key])[0]
        natural = source("小青，该你落子了", "10004" if ix == 0 else "10006", room, 30 + ix, base)
        await reviewed(env, natural, [answer(natural.text, "小青", "name", "circle", "natural", "other")])
        if ix == 0:
            assert not privacy.project(env.store, outsider, [key])[0]
    assert [r["text"] for r in privacy.project(env.store, outsider, [key])[0]] == ["小青"]


async def test_edited_source_invalidates_saved_summary_and_recipient_pins(env):
    from companion_core.errors import AssistantError
    from companion_core.governance import GovernedContext
    from tests_postgres.test_desktop_reply_quota import result
    from tests_postgres.test_windows_role_tools import manager

    now = datetime.now(timezone.utc)
    first = source("公开活动周六开门", "10002", "10002", 710, now, "private")
    other = source("请记下本次答复", "10004", "10004", 711, now, "private")
    auth = ReplyAuthorizations(env.store)
    auth.update(
        ReplyScope("qq", "10001", "private", "10004"), enabled=True, expected_version=None, actor="fixture"
    )
    key = await reviewed(env, first, [answer(first.text, "周六", "ordinary", "social", "general")])
    events.ingest(env.store, other)
    values, pins = privacy.project(env.store, other, [key])
    assert [v["text"] for v in values] == [first.text]
    governed = GovernedContext(env.store, other)
    governed.classify_event()
    governed.fragment_pins.extend(pins)
    governed.freeze()
    doc = {
        "schema_version": 1,
        "id": "derived",
        "kind": "experience",
        "content": first.text,
        "confidence": "observed",
        "locked": False,
        "tags": [],
        "source": {"type": "interaction", "reference": other.id, "recorded_at": other.occurred_at},
    }
    env.store.write("memories", doc, None, "fixture", "derived")
    governed.classify_written("memories", "derived", personal=True)
    assert privacy.raw_allowed(env.store, governed.request, "memories/derived")
    api = manager(env.store, lambda: now.timestamp())
    row = result(api, "privacy.list", {"after": None, "id": pins[0][0]})["items"][0]
    result(api, "privacy.correct", {"id": row["id"], "expected": row["version"], "level": "private"})
    with pytest.raises(AssistantError):
        privacy.check_projection(env.store, other, pins)
    assert not privacy.raw_allowed(env.store, governed.request, "memories/derived")
    assert not any(entry["data"]["id"] == "derived" for entry in governed.memory(first.text)["records"])
    edit = source("已改期，只有我知道新时间", "10002", "10002", 710, now, "private")
    events.ingest(env.store, edit)
    assert privacy.project(env.store, other, [key]) == ([], [])


async def test_review_only_claims_jobs_inside_current_account(env):
    from dataclasses import replace

    from companion_core.governance import GovernanceRepository

    now = datetime.now(timezone.utc)
    foreign = Event(
        "bilibili", "304", "foreign", ActorRef("bilibili", "302"), "dm:x", now.isoformat(), "私密异账号文本"
    )
    events.ingest(env.store, foreign)
    # An archive may point at its source through the policy instead of an event reference.
    resource = "memories/foreign-archive"
    env.store.write(
        "memories",
        {
            "schema_version": 1,
            "id": "foreign-archive",
            "kind": "experience",
            "content": "合成资料",
            "source": {"type": "interaction", "reference": "archive", "recorded_at": foreign.occurred_at},
            "confidence": "observed",
            "tags": [],
            "locked": False,
        },
        None,
        "fixture",
        "foreign-archive",
    )
    repo = GovernanceRepository(env.store)
    policy = repo.policy(resource)
    repo.set_policy(
        replace(
            policy,
            version="archive-source",
            classification="public",
            audience="public",
            conversation=foreign.thread,
        ),
        policy.version,
        "fixture",
    )
    await privacy_review.tick(env.core)
    with env.store.db() as db:
        for key in (resource, "event/" + foreign.id):
            assert tuple(
                db.execute(
                    "SELECT cursor_pos,due,complete FROM privacy_jobs WHERE resource=?", (key,)
                ).fetchone()
            ) == (0, 0, 0)


@pytest.mark.parametrize("shared_worker", [False, True])
async def test_slow_review_does_not_hold_platform_intake(env, shared_worker):
    await handle(env, incoming(1100, text="我要参加一场合成活动"))
    tasks = TaskBarrier()
    runtime = SimpleNamespace(store=env.store, cores={"qq": env.core}, tasks=tasks)
    scheduler = DesktopScheduler(runtime, tick=0.01)
    started = asyncio.Event()
    replied = asyncio.Event()
    release = asyncio.Event()
    original = env.model.generate
    capacity = asyncio.Semaphore(1)
    if shared_worker:
        env.model.capacity = capacity

    async def run(prompt, context, schema, *args, **kw):
        if "current" in context:
            if shared_worker:
                async with capacity:
                    started.set()
                    await release.wait()
            else:
                started.set()
                await release.wait()
            return {"facts": []}
        replied.set()
        if shared_worker:
            async with capacity:
                return await original(prompt, context, schema, *args, **kw)
        return await original(prompt, context, schema, *args, **kw)

    env.model.generate = run
    work = asyncio.create_task(scheduler._privacy_work())
    try:
        await asyncio.wait_for(started.wait(), 5)
        env.module.accept(incoming(1101, text="第二条合成问题"))
        await asyncio.wait_for(scheduler._step("qq"), 5)
        assert replied.is_set() and len(env.provider.sent) == 2
        assert not release.is_set()
    finally:
        release.set()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
    assert scheduler.review_job is None
