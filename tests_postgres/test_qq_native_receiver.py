import secrets
from copy import deepcopy

import pytest

from companion_core import postgres_transfer
from companion_core.contracts import stable_key
from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_qq.native_receiver import NativeReceiver
from tests.test_qq_native_normalization import IDENTITY, raw
from tests_postgres.test_qq_module import env as env  # noqa: F401
from tests_postgres.test_qq_native_windows import bound, message


class Worker:
    def __init__(self, count=3):
        self.upper = bound(count + 1)
        self.messages = [message(n, mention=n == 2) for n in range(2, count + 2)]
        self.calls, self.fail_page = [], None
        self.partial = False

    def request(self, command, **args):
        self.calls.append((command, deepcopy(args)))
        if command == "group_head":
            return deepcopy(self.upper)
        if command == "group_open":
            self.lower, self.frozen_upper = deepcopy(args["lower"]), deepcopy(args["upper"])
            self.window_id = stable_key("test-window", self.lower["seq"], self.frozen_upper["seq"])
            self.frozen = [
                m
                for m in self.messages
                if int(self.lower["seq"]) < int(m["msgSeq"]) <= int(self.frozen_upper["seq"])
            ]
            self.history = {
                "status": "incomplete" if self.partial else "complete",
                "reasons": ["history_query_failed"] if self.partial else [],
            }
            return {
                "window_id": self.window_id,
                "upper": self.frozen_upper,
                "count": len(self.frozen),
                "history_status": self.history,
            }
        assert command == "group_page"
        if args["page"] == self.fail_page:
            raise OSError("injected read interruption")
        page = args["page"]
        return {
            "window_id": self.window_id,
            "kind": "group",
            "target": "10003",
            "lower": self.lower,
            "upper": self.frozen_upper,
            "history_status": self.history,
            "page": page,
            "complete": (page + 1) * 100 >= len(self.frozen),
            "messages": deepcopy(self.frozen[page * 100 : (page + 1) * 100]),
        }


def receiver(env, worker, **kwargs):
    return NativeReceiver(env.module, worker, IDENTITY, guard=env.core.guard, **kwargs)


async def test_first_registration_does_not_import_preexisting_history(env):
    worker = Worker()
    source = receiver(env, worker)
    assert source.poll("group", "10003") == {"status": "registered"}
    assert source.poll("group", "10003") == {"status": "idle"}
    assert [call[0] for call in worker.calls] == ["group_head", "group_head"]
    assert await env.module.runtime_poll() == []


async def test_empty_frozen_range_advances_without_creating_a_reply(env):
    worker = Worker(1)
    worker.messages = []
    source = receiver(env, worker)
    source.enroll("group", "10003", bound(1))
    assert source.poll("group", "10003") == {"status": "staged", "pages": 1, "messages": 0}
    assert source.poll("group", "10003")["complete"] is True
    assert env.store.state(source.key("group", "10003"))["lower"] == bound(2)
    assert await env.module.runtime_poll() == []


async def test_private_same_text_rows_survive_saved_pages_as_distinct_messages(env):
    def private_bound(n):
        return {"accountUin": "10001", "accountUid": "u_self", "peerUin": "10002", "peerUid": "u_peer",
                "senderUin": "10002", "senderUid": "u_peer", "clientSeq": str(5000+n),
                "time": "1792922400", "random": str(100+n)}

    class PrivateWorker:
        def request(self, command, **args):
            if command == "private_head":
                assert args == {"target": "10002"}
                return private_bound(3)
            if command == "private_open":
                assert args == {"target": "10002", "lower": private_bound(1), "upper": private_bound(3)}
                return {"window_id": "private-range", "upper": private_bound(3), "count": 2,
                        "history_status": {"status": "complete", "reasons": []}}
            assert command == "private_page" and args == {"window_id": "private-range", "page": 0}
            return {"window_id": "private-range", "kind": "private", "target": "10002",
                    "lower": private_bound(1), "upper": private_bound(3), "page": 0, "complete": True,
                    "messages": [raw(2), raw(3)], "history_status": {"status": "complete", "reasons": []}}

    source = receiver(env, PrivateWorker(), page_size=1)
    source.enroll("private", "10002", private_bound(1))
    assert source.poll("private", "10002")["messages"] == 2
    source.poll("private", "10002")
    assert await env.module.runtime_poll() == []
    source.poll("private", "10002")
    pending = await env.module.runtime_poll()
    assert len(pending) == 1 and pending[0].visibility == "private"
    batch = env.module.decision_batch(pending[0])
    assert len(batch["members"]) == 2
    assert env.module.native_inbox.count() == 2


async def test_saved_pages_survive_worker_restart_and_do_not_include_later_arrivals(env):
    worker = Worker()
    worker.partial = True
    source = receiver(env, worker, page_size=1)
    source.enroll("group", "10003", bound(1))
    assert source.poll("group", "10003") == {"status": "staged", "pages": 3, "messages": 3}
    assert await env.module.runtime_poll() == []
    with env.store.db() as db:
        rows = list(
            db.execute(
                "SELECT value FROM state WHERE key LIKE ?", (source.key("group", "10003") + ":page:%",)
            )
        )
        assert len(rows) == 3 and all(row["value"].startswith("enc1:") for row in rows)

    class UnusedWorker:
        def request(self, *args, **kwargs):
            raise AssertionError("ready pages must not be read again from native history")

    rebuilt = receiver(env, UnusedWorker(), page_size=1)
    for index in range(3):
        result = rebuilt.poll("group", "10003")
        assert result == {"status": "published", "next_page": index + 1, "complete": index == 2}
        if index < 2:
            assert await env.module.runtime_poll() == []
    pending = await env.module.runtime_poll()
    assert len(pending) == 1
    batch = env.module.decision_batch(pending[0])
    assert len(batch["members"]) == 3 and batch["history_status"]["status"] == "incomplete"
    assert rebuilt.poll("group", "10003") == {"status": "waiting_for_batch"}
    assert (await env.core.handle(pending[0]))["action"]["status"] == "succeeded"
    assert await env.module.runtime_poll() == []
    worker.upper = bound(5)
    worker.messages.append(message(5, mention=True))
    worker.partial = False
    assert source.poll("group", "10003")["messages"] == 1
    source.poll("group", "10003")
    newer = (await env.module.runtime_poll())[0]
    assert len(env.module.decision_batch(newer)["members"]) == 1
    assert len(env.provider.sent) == 1


async def test_collection_restart_keeps_upper_and_rebuilds_only_unpublished_pages(env):
    worker = Worker(101)
    worker.fail_page = 1
    source = receiver(env, worker)
    source.enroll("group", "10003", bound(1))
    with pytest.raises(OSError):
        source.poll("group", "10003")
    assert env.store.state(source.key("group", "10003"))["staged"] == 1
    assert env.module.native_inbox.state("group", "10003") is None
    worker.fail_page = None
    worker.upper = bound(103)
    worker.messages.append(message(103))
    rebuilt = receiver(env, worker)
    result = rebuilt.poll("group", "10003")
    assert result == {"status": "staged", "pages": 2, "messages": 101}
    assert worker.frozen_upper == bound(102)
    assert [call[0] for call in worker.calls].count("group_head") == 1
    assert await env.module.runtime_poll() == []


async def test_crash_after_inbox_commit_replays_identical_saved_page(env, monkeypatch):
    source = receiver(env, Worker(), page_size=1)
    source.enroll("group", "10003", bound(1))
    source.poll("group", "10003")
    original = source._save
    with monkeypatch.context() as patch:
        patch.setattr(
            source, "_save", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("after inbox commit"))
        )
        with pytest.raises(RuntimeError):
            source.poll("group", "10003")
    assert env.module.native_inbox.state("group", "10003")["next_page"] == 1
    assert env.store.state(source.key("group", "10003"))["next_page"] == 0
    assert source._save == original
    assert source.poll("group", "10003")["next_page"] == 1
    assert env.module.native_inbox.count() == 1
    assert await env.module.runtime_poll() == []


async def test_revocation_blocks_saved_pages_and_does_not_advance_position(env):
    source = receiver(env, Worker())
    source.enroll("group", "10003", bound(1))
    source.poll("group", "10003")
    before = env.store.state(source.key("group", "10003"))
    auth = ReplyAuthorizations(env.store)
    scope = ReplyScope("qq", "10001", "group", "10003")
    auth.update(scope, enabled=False, expected_version=auth.version(scope), actor="test")
    with pytest.raises(AssistantError):
        source.poll("group", "10003")
    assert env.store.state(source.key("group", "10003")) == before
    assert env.module.native_inbox.count() == 0


async def test_backup_restores_spool_and_position_without_resuming_authorization(env):
    source = receiver(env, Worker(), page_size=1)
    source.enroll("group", "10003", bound(1))
    source.poll("group", "10003")
    source.poll("group", "10003")
    saved = env.store.state(source.key("group", "10003"))
    packets = [env.store.state(source.key("group", "10003") + ":page:" + str(n)) for n in range(3)]
    env.core.stop()
    await env.module.close()
    key = secrets.token_bytes(32)
    backup = postgres_transfer.create(env.store, key)
    env.core.start()
    source.poll("group", "10003")
    source.poll("group", "10003")
    assert env.store.state(source.key("group", "10003"))["phase"] == "idle"
    env.core.stop()
    await env.module.close()
    postgres_transfer.restore(env.store, backup, key, expected=postgres_transfer.fingerprint(env.store))

    class NoTransport:
        def request(self, *args, **kwargs):
            raise AssertionError("restored ready spool must not access native history")

    restored = receiver(env, NoTransport(), page_size=1)
    assert env.store.state(restored.key("group", "10003")) == saved
    assert env.module.native_inbox.count() == 1
    assert [env.store.state(source.key("group", "10003") + ":page:" + str(n)) for n in range(3)] == packets
    assert ReplyAuthorizations(env.store).version(ReplyScope("qq", "10001", "group", "10003")) is None
    with pytest.raises(AssistantError) as error:
        restored.poll("group", "10003")
    assert error.value.code == "not_running"
    with env.store.db() as db:
        values = list(db.execute("SELECT value FROM state WHERE key LIKE ?",
                                 (restored.key("group", "10003") + ":page:%",)))
        assert len(values) == 3 and all(row[0].startswith("enc1:") for row in values)
