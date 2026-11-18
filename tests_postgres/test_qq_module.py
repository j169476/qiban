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


async def test_explicit_timing_captures_one_complete_reply_pipeline(env):
    from companion_core.reply_timing import control

    assert control(env.store, "report")["status"] == "disabled"
    control(env.store, "arm", "qq")
    result = await handle(env, incoming(701))
    assert result and result[0]["action"]["status"] == "succeeded"
    report = control(env.store, "report")
    assert report["status"] == "completed"
    names = {r["stage"] for r in report["spans"]}
    assert {"reply.total", "reply.lock_wait", "reply.precheck", "context.build",
            "decision.prepare_and_generate", "result.persist_memory", "delivery.dispatch",
            "platform.execute", "delivery.finalize"} <= names
    await handle(env, incoming(702))
    assert control(env.store, "report") == report


@pytest.mark.parametrize("code", ["qq_login_required", "qq_login_timeout", "qq_native_protocol_invalid"])
async def test_native_handshake_failure_keeps_original_error_instead_of_retrying_closed_worker(env, code):
    calls = []

    def fail():
        calls.append(True)
        if len(calls) > 1:
            raise AssistantError("qq_native_disconnected", "closed worker")
        raise AssistantError(code, "synthetic handshake failure")

    await env.module.close()
    env.module.validate_start()
    env.provider.probe = fail
    env.provider.poll_native = lambda *args: []
    with pytest.raises(AssistantError) as error:
        await env.module.runtime_poll()
    assert error.value.code == code
    assert env.module.code == code
    assert calls == [True]


async def test_native_private_recovery_deduplicates_across_caches_and_reports_history(env):
    from tests.test_qq_native_normalization import IDENTITY, raw

    partial = {"status": "incomplete", "reasons": ["checkpoint_unavailable", "history_read_stalled"]}
    for n in (1, 2):
        env.module.accept_native(raw(n), identity=IDENTITY, history_status=partial)
        env.module.accept_native({**raw(n), "msgId": str(999000 + n), "msgSeq": str(888 + n)},
                                 identity=IDENTITY, history_status=partial)
    assert len(env.store.state(env.module.queue_key)) == 2
    anchor = (await env.module.runtime_poll())[0]
    record = env.module.decision_batch(anchor)
    assert len(record["members"]) == 2
    assert record["history_status"] == partial
    # A later complete report applies to subsequent batches without changing
    # the report already frozen for the current model request.
    env.module.accept_native(raw(1), identity=IDENTITY, history_status={"status": "complete", "reasons": []})
    assert env.module.decision_batch(anchor)["history_status"] == partial
    assert (await env.core.handle(anchor))["action"]["status"] == "succeeded"
    assert env.model.calls[-1]["backlog"]["message_count"] == 2
    assert env.model.calls[-1]["history_status"]["complete"] is False
    assert env.store.state(record["key"])["coverage"]["history_status"] == partial
    assert len(env.provider.sent) == 1
    env.module.accept_native({**raw(2), "msgId": "9999999"}, identity=IDENTITY, history_status=partial)
    assert await env.module.runtime_poll() == []
    assert len(env.provider.sent) == 1


async def test_native_merged_media_candidates_are_limited_before_model_and_not_claimed_read(env):
    from tests.test_qq_native_normalization import IDENTITY, raw

    for number in range(1, 9):
        message = raw(number)
        message['elements'].append({'elementType': 2, 'picElement': {}})
        env.module.accept_native(message, identity=IDENTITY)
    anchor = (await env.module.runtime_poll())[0]
    assert len(env.module.decision_batch(anchor)['members']) == 8
    result = await env.core.handle(anchor)
    assert result['action']['status'] == 'succeeded'
    material = env.model.calls[-1]['event_material']
    budget = material['media_budget']
    assert material['attachments_read'] == material['images_read'] == 0
    assert len(budget['candidates']) == 3 and budget['unread'] == 8
    assert {item['code']: item['count'] for item in budget['reasons']} == {'image_count': 5, 'unavailable': 3}
    record = env.module.merged.for_event(anchor)
    # The active batch is retired after the single reply, but its coverage
    # receipt must retain the distinction between candidates and read content.
    assert record is None
    key = env.store.state('merged-anchor:' + anchor.id)
    assert env.store.state(key)['coverage']['media_budget'] == budget
    assert len(env.provider.sent) == 1


async def test_incomplete_history_continues_with_frozen_reason_and_no_cross_thread_leak(env):
    from companion_core import events
    from companion_core.contracts import Event

    env.module.accept(incoming(901, text="readable remainder"))
    event = Event.from_dict(events.get(env.store, env.store.state(env.module.queue_key)[0])["event"])
    partial = {"status": "incomplete", "reasons": ["checkpoint_unavailable"]}
    env.module.merged.record_history(event, partial)
    anchor = (await env.module.runtime_poll())[0]
    record = env.module.decision_batch(anchor)
    assert record["history_status"] == partial
    env.module.merged.record_history(event, {"status": "complete", "reasons": []})
    assert env.module.decision_batch(anchor)["history_status"] == partial
    assert (await env.core.handle(anchor))["action"]["status"] == "succeeded"
    assert env.model.calls[-1]["history_status"]["complete"] is False
    assert env.store.state(record["key"])["coverage"]["history_status"] == partial
    assert await env.module.runtime_poll() == []
    assert len(env.provider.sent) == 1
    env.module.accept(incoming(902, group=True, mention=True))
    other = (await env.module.runtime_poll())[0]
    assert (await env.core.handle(other))["action"]["status"] == "succeeded"
    assert "history_status" not in env.model.calls[-1]


async def test_new_arrivals_during_generation_stay_in_next_frozen_batch(env):
    env.module.accept(incoming(201, text='first'))
    env.module.accept(incoming(202, text='second'))
    anchor = (await env.module.runtime_poll())[0]
    original = env.model.generate
    async def generate(*args, **kwargs):
        env.module.accept(incoming(203, text='third-during-generation'))
        again = await env.module.runtime_poll()
        assert [event.id for event in again] == [anchor.id]
        return await original(*args, **kwargs)
    env.model.generate = generate
    assert (await env.core.handle(anchor))['action']['status'] == 'succeeded'
    assert env.model.calls[0]['backlog']['message_count'] == 2
    assert 'third-during-generation' not in str(env.model.calls[0])
    env.model.generate = original
    following = await env.module.runtime_poll()
    assert len(following) == 1 and following[0].id != anchor.id
    assert (await env.core.handle(following[0]))['action']['status'] == 'succeeded'
    assert env.model.calls[-1]['backlog']['message_count'] == 1
    assert len(env.provider.sent) == 2


async def test_confirmed_send_before_batch_receipt_is_recovered_without_model_or_resend(env, monkeypatch):
    from companion_core import delivery
    env.module.accept(incoming(211))
    env.module.accept(incoming(212))
    anchor = (await env.module.runtime_poll())[0]
    def interrupted(*args, **kwargs):
        raise RuntimeError('synthetic interruption after confirmed send')
    with monkeypatch.context() as patch:
        patch.setattr(delivery, 'finish', interrupted)
        with pytest.raises(RuntimeError):
            await env.core.handle(anchor)
    assert len(env.provider.sent) == len(env.model.calls) == 1
    assert await env.module.runtime_poll() == []
    assert len(env.provider.sent) == len(env.model.calls) == 1
    key = env.store.state('merged-anchor:' + anchor.id)
    record = env.store.state(key)
    assert record['status'] == 'completed' and record['outcome'] == 'succeeded'
    assert all(env.store.state('merged-consumed:' + identifier)['coverage'] == 'complete'
               for identifier, _ in record['members'])


async def test_historical_group_flood_pins_trigger_cuts_before_model_and_replies_once(env):
    from companion_core.context_budget import encoded
    for index in range(40):
        raw = incoming(300 + index, group=True, mention=index == 2, text=f'message-{index}:' + '水群' * 500)
        raw['time'] = STAMP - 86400 + index
        env.module.accept(raw)
    anchor = (await env.module.runtime_poll())[0]
    assert (await env.core.handle(anchor))['action']['status'] == 'succeeded'
    decisions = [context for context in env.model.calls if 'backlog' in context]
    assert len(decisions) == 1
    context = decisions[0]
    assert len(encoded({'units': context['backlog']['units']})) <= 12000
    assert context['backlog']['selection']['trigger_id'] == anchor.id
    assert context['backlog']['selection']['omitted_messages'] > 0
    assert any(unit['id'] == anchor.id and not unit['truncated'] for unit in context['backlog']['units'])
    assert len(env.provider.sent) == 1
    assert all(len(call['query']) <= 12000 for call in env.model.calls if 'query' in call)
    assert env.store.state(env.module.participation.window_key(env.module.scope(anchor))) is None
    record = env.store.state(env.store.state('merged-anchor:' + anchor.id))
    omitted = [identifier for identifier, _ in record['members']
               if env.store.state('merged-consumed:' + identifier)['coverage'] == 'omitted']
    assert omitted and all(env.store.state('core-processed:' + identifier) is None for identifier in omitted)
    assert await env.module.runtime_poll() == []
    env.module.accept(incoming(399, group=True))
    assert await env.module.runtime_poll() == []


async def test_source_correction_during_generation_invalidates_batch_before_send(env):
    from dataclasses import replace

    from companion_core import events
    env.module.accept(incoming(401, text='before'))
    anchor = (await env.module.runtime_poll())[0]
    first = env.module.decision_batch(anchor)
    original = env.model.generate
    async def generate(*args, **kwargs):
        events.ingest(env.store, replace(anchor, text='corrected'))
        return await original(*args, **kwargs)
    env.model.generate = generate
    with pytest.raises(AssistantError):
        await env.core.handle(anchor)
    assert env.provider.sent == []
    env.model.generate = original
    current = (await env.module.runtime_poll())[0]
    assert current.text == 'corrected'
    assert env.store.state(first['key'])['status'] == 'superseded'
    assert (await env.core.handle(current))['action']['status'] == 'succeeded'


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
        for raw in (incoming(102), incoming(103, group=True, mention=True))]
    assert len({event.thread for event in batch}) == 2
    await handle_batch(env.core, batch)
    following = await env.module.runtime_poll()
    assert following == []
    assert len(env.provider.sent) == 2
    private = next(context for context in env.model.calls if context['event']['visibility'] == 'private')
    assert private['backlog']['message_count'] == 2
    await handle_batch(env.core, following)
    assert len(env.provider.sent) == 2


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
async def test_restart_resumes_frozen_batch_and_group_catches_up_once(env, tmp_path, group):
    env.module.accept(incoming(1, group=group, mention=group))
    first = (await env.module.runtime_poll())[0]
    env.core.stop("restart-test")
    await env.module.close()
    replacement = QQModule(env.store, tmp_path, options=OPTIONS, factory=lambda: env.provider)
    core = Companion(env.store, Model(), Host(), [replacement], clock=env.core.clock, run_key="core-run:qq")
    core.start()
    try:
        remaining = await replacement.runtime_poll()
        assert remaining[0].id == first.id
        assert (await core.handle(remaining[0]))["action"]["status"] == "succeeded"
        assert await replacement.runtime_poll() == []
        assert len(env.provider.sent) == 1
        if group:
            replacement.accept(incoming(2, group=True))
            assert await replacement.runtime_poll() == []
            replacement.accept(incoming(3, group=True, mention=True))
            assert len(await replacement.runtime_poll()) == 1
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
