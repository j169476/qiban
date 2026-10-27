import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.runtime import Companion
from companion_qq.module import QQModule
from tests_postgres.test_wechat_role import Host, Model

STAMP = 1792922400
OPTIONS = {"version": 1, "account": "10001", "private_targets": ["10002"], "groups": ["10003"],
           "http": "http://127.0.0.1:3000", "ws": "ws://127.0.0.1:3001"}


def incoming(n, *, group=False, mention=False, sender="10002", text="same text", quote=None):
    segments = [{"type": "text", "data": {"text": text}}]
    if mention:
        segments.append({"type": "at", "data": {"qq": "10001"}})
    if quote is not None:
        segments.append({"type": "reply", "data": {"id": quote}})
    return {"post_type": "message", "self_id": 10001, "message_type": "group" if group else "private",
            "sub_type": "normal" if group else "friend", "user_id": sender, "group_id": 10003,
            "time": STAMP, "message_id": n, "message": segments}


class Socket:
    def __init__(self):
        self.queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self):
        self.queue.put_nowait(None)


class Provider:
    def __init__(self):
        self.sent = []
        self.unknown = False
        self.before = lambda: None
        self.wrong_account = False
        self.sockets = []

    def probe(self):
        self.identity()
        return {"account": "10001", "account_label": "synthetic", "private_targets": ["10002"], "groups": ["10003"]}

    def identity(self):
        if self.wrong_account:
            raise AssistantError("qq_account_mismatch", "synthetic")
        return {"user_id": 10001}

    async def connect(self):
        socket = Socket()
        self.sockets.append(socket)
        return socket

    def send(self, scope, text):
        self.before()
        self.sent.append((scope, text))
        if self.unknown:
            raise TimeoutError()
        return str(5000 + len(self.sent))

    def close(self):
        pass


@pytest.fixture
async def env(pg_store, tmp_path):
    provider = Provider()
    module = QQModule(pg_store, tmp_path, options=OPTIONS, factory=lambda: provider)
    module.probe()
    for target in module.reply_targets():
        ReplyAuthorizations(pg_store).update(target.scope, enabled=True, expected_version=None, actor="test")
    model = Model()
    core = Companion(pg_store, model, Host(), [module], clock=lambda: datetime.fromtimestamp(STAMP, timezone.utc), run_key="core-run:qq")
    core.start()
    await module.runtime_poll()
    yield SimpleNamespace(store=pg_store, module=module, core=core, model=model, provider=provider)
    core.stop()
    await module.close()


async def handle(env, raw):
    env.module.accept(raw)
    batch = await env.module.runtime_poll()
    return [await env.core.handle(event) for event in batch]


@pytest.mark.asyncio
async def test_private_replies_dedupe_identical_bubbles_and_group_scope(env):
    assert (await handle(env, incoming(1)))[0]["action"]["status"] == "succeeded"
    assert await handle(env, incoming(1)) == []
    assert (await handle(env, incoming(2)))[0]["action"]["status"] == "succeeded"
    assert await handle(env, incoming(1, group=True)) == []
    assert len(env.provider.sent) == 2 and len(env.model.calls) == 2
    assert await handle(env, incoming(3, sender="10004")) == []


@pytest.mark.asyncio
async def test_poll_batches_different_conversations_without_overtaking_same_chat(env):
    from companion_core.reply_batch import handle_batch

    for raw in (incoming(101), incoming(102), incoming(103, group=True, mention=True)):
        env.module.accept(raw)
    batch = await env.module.runtime_poll()
    from companion_qq.normalization import normalize

    assert [event.id for event in batch] == [normalize(raw, OPTIONS, sent_reference=lambda *_: False).id
        for raw in (incoming(101), incoming(103, group=True, mention=True))]
    assert len({event.thread for event in batch}) == 2
    await handle_batch(env.core, batch)
    following = await env.module.runtime_poll()
    assert [event.id for event in following] == [normalize(incoming(102), OPTIONS, sent_reference=lambda *_: False).id]
    await handle_batch(env.core, following)
    assert len(env.provider.sent) == 3


@pytest.mark.asyncio
async def test_group_wake_any_member_five_replies_then_requires_new_trigger(env):
    assert await handle(env, incoming(1, group=True, text="@小龙娘")) == []
    for number in range(2, 7):
        result = await handle(env, incoming(number, group=True, mention=number == 2, sender="10004"))
        assert result[0]["action"]["status"] == "succeeded", result
    assert await handle(env, incoming(7, group=True)) == []
    assert (await handle(env, incoming(8, group=True, quote="5005")))[0]["action"]["status"] == "succeeded"
    assert len(env.provider.sent) == 6
    scope = ReplyScope("qq", "10001", "private", "10002")
    assert env.module.participation.usage(scope)[1]["confirmed"] == 0


@pytest.mark.asyncio
async def test_uncertain_send_is_not_retried_and_account_mismatch_prevents_send(env):
    env.provider.wrong_account = True
    result = await handle(env, incoming(1))
    assert result[0]["action"]["status"] == "cancelled" and env.provider.sent == []
    env.provider.wrong_account = False
    env.provider.unknown = True
    result = await handle(env, incoming(2))
    assert result[0]["action"]["status"] == "uncertain" and len(env.provider.sent) == 1
    assert env.module.tainted


@pytest.mark.asyncio
async def test_topic_end_is_local_action_and_private_cannot_end_group(env):
    await handle(env, incoming(1, group=True, mention=True))

    async def end(*_args, **_kwargs):
        return {"note": "topic ended", "action": {"capability": "conversation.end", "arguments": {}},
                "memories": [], "identity_proposals": [], "relationship": None}

    env.model.generate = end
    result = await handle(env, incoming(2, group=True))
    assert result[0]["action"]["receipt"]["participation_ended"]
    assert len(env.provider.sent) == 1
    assert await handle(env, incoming(3, group=True)) == []
    result = await handle(env, incoming(4))
    assert result[0]["action"]["status"] == "cancelled" and len(env.provider.sent) == 1


@pytest.mark.asyncio
async def test_default_denial_and_revocation_before_submit(env):
    target = next(t for t in env.module.reply_targets() if t.scope.kind == "private")
    permissions = ReplyAuthorizations(env.store)
    old = permissions.record(target.scope)
    permissions.update(target.scope, enabled=False, expected_version=old["version"], actor="test")
    assert await handle(env, incoming(1)) == [] and not env.model.calls
    value = permissions.record(target.scope)
    permissions.update(target.scope, enabled=True, expected_version=value["version"], actor="test")
    assert await handle(env, incoming(1)) == []  # Denied arrival is never replayed after grant.
    env.module.accept(incoming(2))
    event = (await env.module.runtime_poll())[0]
    value = permissions.record(target.scope)
    permissions.update(target.scope, enabled=False, expected_version=value["version"], actor="test")
    with pytest.raises(AssistantError):
        await env.core.handle(event)
    assert not env.provider.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_restart_restores_fresh_private_queue_but_group_requires_new_wake(env, tmp_path, group):
    env.module.accept(incoming(1, group=group, mention=group))
    first = (await env.module.runtime_poll())[0]
    env.core.stop("restart-test")
    await env.module.close()
    replacement = QQModule(env.store, tmp_path, options=OPTIONS, factory=lambda: env.provider)
    core = Companion(env.store, Model(), Host(), [replacement], clock=env.core.clock, run_key="core-run:qq")
    core.start()
    try:
        remaining = await replacement.runtime_poll()
        if group:
            assert remaining == []
            replacement.accept(incoming(2, group=True))
            assert await replacement.runtime_poll() == []
            replacement.accept(incoming(3, group=True, mention=True))
            remaining = await replacement.runtime_poll()
            assert remaining[0].id != first.id
        else:
            assert remaining[0].id == first.id
        assert (await core.handle(remaining[0]))["action"]["status"] == "succeeded"
        assert await replacement.runtime_poll() == []
        assert len(env.provider.sent) == 1
    finally:
        core.stop()
        await replacement.close()


@pytest.mark.asyncio
async def test_event_reader_reconnect_retains_encrypted_queue_and_dedupes_replay(env):
    import json

    socket = env.provider.sockets[-1]
    socket.queue.put_nowait(json.dumps(incoming(1)))
    socket.queue.put_nowait(json.dumps(incoming(1)))
    socket.queue.put_nowait(None)
    await asyncio.wait_for(env.module.reader, 2)
    assert not env.module.ready and env.module.retry_at > 0
    assert len(env.store.state(env.module.queue_key)) == 1
    env.module.retry_at = 0  # Advance deterministic reconnect deadline, no long wait.
    pending = await env.module.runtime_poll()
    assert env.module.ready and len(env.provider.sockets) == 2
    assert (await env.core.handle(pending[0]))["action"]["status"] == "succeeded"
    assert await handle(env, incoming(1)) == [] and len(env.provider.sent) == 1
    with env.store.db() as db:
        assert all(row[0].startswith("enc1:") for row in db.execute("SELECT body FROM core_events"))


@pytest.mark.asyncio
async def test_reconnect_to_other_logged_in_account_blocks_pending_reply(env):
    env.module.accept(incoming(1))
    await env.provider.sockets[-1].close()
    await asyncio.wait_for(env.module.reader, 2)
    env.provider.wrong_account = True
    env.module.retry_at = 0
    with pytest.raises(AssistantError) as error:
        await env.module.runtime_poll()
    assert error.value.code == "qq_account_mismatch"
    assert not env.module.ready and not env.provider.sent and not env.model.calls
    assert len(env.store.state(env.module.queue_key)) == 1


@pytest.mark.asyncio
async def test_wrong_self_id_from_event_channel_stops_before_model(env):
    import json

    env.provider.sockets[-1].queue.put_nowait(json.dumps(incoming(1) | {"self_id": 10002}))
    await asyncio.wait_for(env.module.reader, 2)
    assert env.module.code == "qq_account_mismatch"
    with pytest.raises(AssistantError) as error:
        await env.module.runtime_poll()
    assert error.value.code == "qq_account_mismatch"
    assert not env.model.calls and not env.provider.sent and not env.store.state(env.module.queue_key)
