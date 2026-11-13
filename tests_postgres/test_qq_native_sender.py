import pytest

from companion_core.errors import AssistantError
from companion_qq.native_sender import NativeSender
from tests_postgres.test_qq_module import env as env  # noqa: F401
from tests_postgres.test_qq_module import incoming

IDENTITY = {"uin": "10001", "uid": "u_self"}


class Worker:
    def __init__(self, store):
        self.store, self.calls, self.submits = store, [], 0
        self.closed = self.lose_receipt = False
        self.kind = None

    def request(self, command, **args):
        self.calls.append(command)
        if command == "identity":
            return IDENTITY
        if command == "prepare_text":
            self.kind = args["kind"]
            return {"token": "a" * 64, "message_id": "9001"}
        assert command == "submit_text"
        with self.store.db() as db:
            actions = list(db.execute("SELECT id FROM core_actions WHERE status='dispatching'"))
        assert len(actions) == 1
        persisted = self.store.state("qq-native-send:" + actions[0]["id"])
        assert persisted["status"] == "attempted" and persisted["message_id"] == "9001"
        self.submits += 1
        if self.lose_receipt:
            raise TimeoutError("synthetic receipt lost after native submit")
        return {"message_id": "9001", "reference": "native-seq:701" if self.kind == "group" else None}

    def close(self):
        self.closed = True


def install(env):
    worker = Worker(env.store)
    sender = NativeSender(env.store, worker, IDENTITY)
    env.provider.send_action = sender.send_action
    return worker, sender


async def execute(env, *, group=False):
    env.module.accept(incoming(501, group=group, mention=group))
    event = (await env.module.runtime_poll())[0]
    result = await env.core.handle(event)
    return event, result["action"]


async def test_native_send_commits_intent_first_then_saves_receipt_and_quote_reference(env):
    worker, sender = install(env)
    event, action = await execute(env, group=True)
    assert action["status"] == "succeeded" and worker.submits == 1
    assert env.store.state(env.module.sent_key("group", "10003", "native-seq:701"))["action"] == action["id"]
    with env.store.db() as db:
        row = db.execute("SELECT body FROM core_actions WHERE id=?", (action["id"],)).fetchone()
        assert row["body"].startswith("enc1:")
        text = env.store.ledger_decode("core_actions.body", row["body"])["arguments"]["text"]
    count = len(worker.calls)
    restored = NativeSender(env.store, worker, IDENTITY)
    assert restored.send_action(action["id"], env.module.scope(event), text).message_id == "9001"
    assert len(worker.calls) == count
    with pytest.raises(AssistantError, match="") as exc:
        restored.send_action(action["id"], env.module.scope(event), "different text")
    assert exc.value.code == "qq_action_mismatch"


async def test_lost_receipt_is_terminal_uncertain_after_sender_reconstruction(env):
    worker, _ = install(env)
    worker.lose_receipt = True
    event, action = await execute(env)
    assert action["status"] == "uncertain" and worker.submits == 1 and worker.closed
    state = env.store.state("qq-native-send:" + action["id"])
    assert state["status"] == "attempted" and "receipt" not in state
    with env.store.db() as db:
        row = db.execute("SELECT body FROM core_actions WHERE id=?", (action["id"],)).fetchone()
        text = env.store.ledger_decode("core_actions.body", row["body"])["arguments"]["text"]
    another = Worker(env.store)
    with pytest.raises(AssistantError) as exc:
        NativeSender(env.store, another, IDENTITY).send_action(action["id"], env.module.scope(event), text)
    assert exc.value.code == "qq_send_unconfirmed" and another.calls == []


@pytest.mark.parametrize("fail_status,expected_submits", [("attempted", 0), ("confirmed", 1)])
async def test_journal_failure_closes_native_transport_without_resending(env, monkeypatch, fail_status, expected_submits):
    worker, _ = install(env)
    original = env.store.set_state

    def fail(key, value, *args, **kwargs):
        if key.startswith("qq-native-send:") and value.get("status") == fail_status:
            raise RuntimeError("injected journal write failure")
        return original(key, value, *args, **kwargs)

    monkeypatch.setattr(env.store, "set_state", fail)
    _, action = await execute(env)
    assert action["status"] == "uncertain" and worker.submits == expected_submits and worker.closed
    state = env.store.state("qq-native-send:" + action["id"])
    assert state is None if fail_status == "attempted" else state["status"] == "attempted"


async def test_native_sender_cannot_submit_without_a_core_dispatch_action(env):
    worker, sender = install(env)
    scope = env.module.reply_targets()[0].scope
    with pytest.raises(AssistantError) as exc:
        sender.send_action("missing-action", scope, "text")
    assert exc.value.code == "qq_action_missing" and worker.calls == []
