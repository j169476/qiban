"""Single-conversation reply exclusions: precedence, invalidation and new-message-only lift.

Every account, conversation, timestamp and message below is synthetic. No connector,
platform process, model or real chat is touched; the store is the disposable
PostgreSQL schema from the shared fixture.
"""
from dataclasses import asdict

import pytest

from companion_core.errors import AssistantError
from companion_core.reply_audience import Audience, LiveMembers, read, registry
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.reply_exclusion import AUDIT_PREFIX, PREFIX, ReplyExclusions, seconds
from companion_core.reply_policy import Participation
from companion_qq.module import QQModule
from tests_postgres.test_qq_module import STAMP
from tests_postgres.test_qq_module import env as qq_env  # noqa: F401
from tests_postgres.test_reply_audiences import set_mode

__all__ = ["qq_env"]


def exclusion(store, scope, *, excluded, at):
    """Write one exclusion exactly as the management entry point does, at a fixed clock."""
    repo = ReplyExclusions(store, clock=lambda: at)
    return repo.update(scope, excluded=excluded, expected_version=repo.record(scope)["version"],
                       actor="synthetic-admin")


def granted(store, scope):
    return ReplyAuthorizations(store).update(scope, enabled=True, expected_version=None, actor="synthetic")


def wechat_scope(env, alias="owner_main"):
    """The scope the WeChat session itself uses for a registered conversation alias."""
    binding = env.receiver.bindings.get(alias)
    return ReplyScope("wechat", env.receiver.bindings.identity, binding.kind, binding.target_ref)


# --- core rules -----------------------------------------------------------------


@pytest.mark.parametrize("platform", ["qq", "wechat"])
@pytest.mark.parametrize("kind", ["private", "group"])
def test_a_scope_without_an_exclusion_keeps_its_previous_permission_token(pg_store, platform, kind):
    store = pg_store
    scope = ReplyScope(platform, "account-a", kind, "12345")
    rules = ReplyAuthorizations(store)
    token = granted(store, scope)["version"]

    repo = ReplyExclusions(store)
    assert repo.record(scope) == {"id": scope.id, "scope": asdict(scope), "excluded": False,
                                  "version": None, "since": None, "not_before": None}
    assert repo.records() == []
    assert rules.version(scope) == token
    assert rules.check(scope, token) is None
    # Nothing was excluded for this scope, so no message time is refused for that reason.
    assert repo.accepts(scope, None) is True and repo.accepts(scope, 1) is True


def test_exclusion_wins_over_the_manual_pick_and_allow_all_for_that_scope_only(pg_store):
    store = pg_store
    rules = ReplyAuthorizations(store)
    scope = ReplyScope("qq", "account-a", "private", "12345")
    sibling = ReplyScope("qq", "account-a", "private", "23456")
    other_account = ReplyScope("qq", "account-b", "private", "12345")
    other_kind = ReplyScope("qq", "account-a", "group", "12345")
    other_platform = ReplyScope("wechat", "account-a", "private", "12345")
    for item in (scope, sibling, other_account, other_kind, other_platform):
        granted(store, item)

    category = Audience("qq", "account-a", "private")
    policy = set_mode(store, category, True)
    live = registry(store)[category] = LiveMembers()
    live.replace(policy["version"], {"12345": "uid-1", "23456": "uid-2"})
    before = rules.version(scope)
    # Under allow-all the effective token is the live member token, not the manual one.
    assert before and before != rules.record(scope)["version"]

    saved = exclusion(store, scope, excluded=True, at=1000)
    assert saved["excluded"] is True and saved["since"] == 1000 and saved["not_before"] is None
    assert rules.version(scope) is None
    with pytest.raises(AssistantError) as blocked:
        rules.check(scope, before)
    assert blocked.value.code == "reply_not_authorized"
    assert Participation(store).admit(scope, "synthetic-event", "direct", "run-1") is None
    # Every other conversation, account, category and platform is untouched.
    assert rules.version(sibling) and rules.version(other_account)
    assert rules.version(other_kind) and rules.version(other_platform)
    assert live.token(policy["version"], "23456") is not None


def test_lift_issues_a_new_permit_and_old_messages_never_become_new(pg_store):
    store = pg_store
    rules = ReplyAuthorizations(store)
    scope = ReplyScope("qq", "account-a", "private", "12345")
    original = granted(store, scope)["version"]
    repo = ReplyExclusions(store)

    exclusion(store, scope, excluded=True, at=1000)
    assert rules.version(scope) is None
    assert exclusion(store, scope, excluded=False, at=1010)["not_before"] == 1010

    current = rules.version(scope)
    # A permit issued before the exclusion can never become valid again.
    assert current and current not in (original, None)
    with pytest.raises(AssistantError) as stale:
        rules.check(scope, original)
    assert stale.value.code == "reply_not_authorized"
    # Only messages the platform timestamped in or after the lift second are admitted.
    assert rules.version(scope, message_time=1009) is None
    assert rules.version(scope, message_time=1010) == current
    assert repo.accepts(scope, 1009.999) is False and repo.accepts(scope, 1010) is True
    # A missing or malformed timestamp is never read as "now".
    assert repo.accepts(scope, None) is False
    assert repo.accepts(scope, seconds("not-a-time")) is False
    assert repo.accepts(scope, seconds("2026-11-08T00:00:00")) is False
    # A scope-level check without a message time still yields the same permit, which is
    # what the final synchronous submission compares against its ticket.
    assert rules.version(scope) == current
    assert Participation(store).permissions.check(scope, current) is None


def test_lift_uses_whole_platform_seconds_and_never_moves_the_start_backwards(pg_store):
    store = pg_store
    scope = ReplyScope("wechat", "account-a", "group", "999@chatroom")
    rules = ReplyAuthorizations(store)
    granted(store, scope)
    repo = ReplyExclusions(store)

    exclusion(store, scope, excluded=True, at=2000.25)
    first = exclusion(store, scope, excluded=False, at=2000.25)
    # Both transports report whole seconds; never accept the second before the lift.
    assert first["not_before"] == 2001
    assert repo.accepts(scope, 2000) is False and repo.accepts(scope, 2001) is True
    assert rules.version(scope, message_time=2000) is None
    assert rules.version(scope, message_time=2001) is not None

    later = exclusion(store, scope, excluded=True, at=2020)
    assert later["since"] == 2020 and later["not_before"] == 2001
    backwards = exclusion(store, scope, excluded=False, at=2005)
    assert backwards["not_before"] == 2020  # A slow clock never moves the start back.
    assert repo.accepts(scope, 2019) is False and repo.accepts(scope, 2020) is True


def test_exclusion_is_strict_encrypted_audited_and_idempotent(pg_store):
    store = pg_store
    scope = ReplyScope("qq", "10001", "private", "10002")
    repo = ReplyExclusions(store)

    for excluded, actor in (("yes", "synthetic"), (True, "")):
        with pytest.raises(AssistantError) as invalid:
            repo.update(scope, excluded=excluded, expected_version=None, actor=actor)
        assert invalid.value.code == "reply_exclusion_invalid"
    first = exclusion(store, scope, excluded=True, at=1000)
    assert isinstance(first["version"], str) and first["version"]
    with pytest.raises(AssistantError) as conflict:
        repo.update(scope, excluded=False, expected_version=None, actor="synthetic")
    assert conflict.value.code == "edit_conflict"
    # Repeating the same state is not a new revision and invalidates nothing.
    assert exclusion(store, scope, excluded=True, at=1100) == first
    with store.db() as db:
        rows = db.execute("SELECT value FROM state WHERE key LIKE ? OR key LIKE ?",
                          (PREFIX + "%", AUDIT_PREFIX + "%")).fetchall()
        assert len(rows) == 2
        assert all(row[0].startswith("enc1:") and "10002" not in row[0] for row in rows)
    assert [record["id"] for record in repo.records()] == [scope.id]

    # A damaged record must never read as "not excluded".
    store.set_state(PREFIX + scope.id, {"id": scope.id, "scope": asdict(scope), "excluded": True,
                                        "version": "v1", "since": None, "not_before": None})
    with pytest.raises(AssistantError) as corrupt:
        repo.record(scope)
    assert corrupt.value.code == "reply_exclusion_corrupt"


def test_exclusion_survives_member_changes_range_switches_restart_and_restore(pg_store):
    store = pg_store
    scope = ReplyScope("wechat", "account-a", "private", "peer")
    rules = ReplyAuthorizations(store)
    grant = granted(store, scope)
    exclusion(store, scope, excluded=True, at=1000)

    # Leaving and coming back keeps the administrator's exclusion.
    with store.db() as db:
        rules.observe_member(scope, False, grant["version"], db)
        rules.observe_member(scope, True, rules.record(scope)["version"], db)
    assert ReplyExclusions(store).record(scope)["excluded"] is True

    # Switching the account-wide range either way keeps it too.
    category = Audience("wechat", "account-a", "private")
    set_mode(store, category, True)
    set_mode(store, category, False)
    kept = ReplyExclusions(store).record(scope)
    assert kept["excluded"] is True and rules.version(scope) is None
    # A restart re-reads the same durable record instead of clearing it.
    assert ReplyExclusions(store).record(scope) == kept

    # Restoring a backup drops the authorization but never lifts an exclusion.
    rules.reset_after_restore()
    assert read(store, category)["enabled"] is False
    assert rules.record(scope)["enabled"] is False
    assert ReplyExclusions(store).record(scope)["excluded"] is True
    assert rules.version(scope) is None


def test_two_accounts_using_the_same_target_stay_independent(pg_store):
    store = pg_store
    rules = ReplyAuthorizations(store)
    first = ReplyScope("qq", "10001", "private", "10002")
    second = ReplyScope("qq", "20001", "private", "10002")
    granted(store, first)
    kept = granted(store, second)["version"]

    exclusion(store, first, excluded=True, at=1000)
    assert rules.version(first) is None and rules.version(second) == kept
    exclusion(store, second, excluded=True, at=1000)
    exclusion(store, first, excluded=False, at=1010)
    assert rules.version(first) is not None and rules.version(second) is None


def test_seconds_rejects_every_unusable_time_and_never_falls_back_to_now(pg_store):
    store = pg_store
    scope = ReplyScope("qq", "account-a", "private", "12345")
    granted(store, scope)
    rules = ReplyAuthorizations(store)
    repo = ReplyExclusions(store)
    exclusion(store, scope, excluded=True, at=1000)
    exclusion(store, scope, excluded=False, at=1010)

    invalid = (None, "", "not-a-time", "2026-11-08T00:00:00", "１０００", True, False, [], {}, 0, -5,
               float("nan"), float("inf"), float("-inf"))
    for value in invalid:
        assert seconds(value) is None, value
        assert repo.accepts(scope, value) is False, value
        if value is not None:
            # No invalid message time can ever produce a permit after a lift. (A None
            # message_time means a scope-level check, not a message timestamp.)
            assert rules.version(scope, message_time=value) is None, value
    # A valid whole second at or after the lift is still accepted.
    assert seconds(1010) == 1010.0 and repo.accepts(scope, 1010) is True
    assert seconds("1010") == 1010.0
    # Real source timestamps behave the same way around a real lift second.
    real = seconds("2026-11-08T00:00:00+00:00")
    later = ReplyScope("qq", "account-a", "private", "54321")
    granted(store, later)
    exclusion(store, later, excluded=True, at=real - 60)
    exclusion(store, later, excluded=False, at=real)
    assert repo.accepts(later, real) is True
    assert repo.accepts(later, real - 1) is False
    assert repo.accepts(later, seconds("2026-11-07T23:00:00+00:00")) is False
    # No invalid time grants a ticket, and an excluded scope admits nothing at all.
    assert Participation(store).admit(scope, "synthetic", "direct", "run", message_time=float("inf")) is None
    assert Participation(store).admit(scope, "synthetic", "direct", "run", message_time=1010) is not None
    exclusion(store, scope, excluded=True, at=1020)
    assert repo.accepts(scope, 1030) is False
    assert Participation(store).admit(scope, "synthetic", "direct", "run", message_time=1030) is None


# --- QQ intake, admission and final submission --------------------------------


@pytest.mark.asyncio
async def test_qq_queued_and_excluded_messages_are_retired_and_only_new_ones_reply(qq_env):
    from tests_postgres.test_qq_module import STAMP, incoming

    env = qq_env
    scope = ReplyScope("qq", "10001", "private", "10002")
    # Accepted while authorized and still queued when the exclusion is written.
    env.module.accept({**incoming(1), "time": STAMP - 90})
    assert len(env.store.state(env.module.queue_key)) == 1

    exclusion(env.store, scope, excluded=True, at=STAMP - 75)
    # A message that arrives while excluded is recorded as seen but never ingested.
    env.module.accept({**incoming(2), "time": STAMP - 60})
    assert await env.module.runtime_poll() == []
    exclusion(env.store, scope, excluded=False, at=STAMP - 60)
    # Neither the queued pre-exclusion message nor the excluded-period message replies.
    assert await env.module.runtime_poll() == []
    assert env.provider.sent == []

    # Only a message timestamped after the lift is admitted and answered.
    env.module.accept({**incoming(3), "time": STAMP - 30})
    pending = await env.module.runtime_poll()
    assert len(pending) == 1
    assert (await env.core.handle(pending[0]))["action"]["status"] == "succeeded"
    assert len(env.provider.sent) == 1


@pytest.mark.asyncio
async def test_qq_exclusion_at_the_last_barrier_prevents_the_native_submit(qq_env):
    from tests_postgres.test_qq_module import STAMP, incoming

    env = qq_env
    scope = ReplyScope("qq", "10001", "private", "10002")
    env.module.accept({**incoming(1), "time": STAMP})
    event = (await env.module.runtime_poll())[0]
    identity = env.provider.identity

    def changed():
        exclusion(env.store, scope, excluded=True, at=STAMP + 1)
        return identity()

    env.provider.identity = changed
    result = await env.core.handle(event)
    assert result["action"]["status"] == "cancelled"
    assert env.provider.sent == []
    assert env.store.state("reply-send:" + result["action"]["id"], None) is None


@pytest.mark.asyncio
async def test_qq_group_catchup_never_replays_history_after_a_lift(qq_env):
    import companion_core.reply_audience as audience_module
    from tests.test_qq_native_normalization import IDENTITY
    from tests_postgres.test_qq_module import STAMP
    from tests_postgres.test_qq_native_windows import message, packet

    env = qq_env
    scope = ReplyScope("qq", "10001", "group", "10003")
    original = audience_module.time.time
    audience_module.time.time = lambda: STAMP - 10
    try:
        category = Audience("qq", "10001", "group")
        policy = set_mode(env.store, category, True)
    finally:
        audience_module.time.time = original
    live = registry(env.store)[category] = LiveMembers()
    live.replace(policy["version"], {"10003": "10003"})

    exclusion(env.store, scope, excluded=True, at=STAMP - 8)
    exclusion(env.store, scope, excluded=False, at=STAMP - 4)
    page = packet([message(2, mention=True) | {"msgTime": str(STAMP - 6)},
                   message(3, mention=True) | {"msgTime": str(STAMP - 2)}],
                  lower=1, upper=3, complete=True)
    env.module.accept_native_page(page, identity=IDENTITY, catchup=True)
    # The page spans the lift; only the newer message becomes reply input.
    assert env.module.native_inbox.count() == 1
    pending = await env.module.runtime_poll()
    assert len(pending) == 1
    batch = env.module.decision_batch(pending[0])
    assert batch["catchup"] is True and len(batch["members"]) == 1
    assert (await env.core.handle(pending[0]))["action"]["status"] == "succeeded"
    assert len(env.provider.sent) == 1
    assert (await env.module.runtime_poll()) == []


@pytest.mark.asyncio
async def test_qq_uncertain_receipt_is_never_resent_after_an_exclusion_and_lift(qq_env):
    from tests_postgres.test_qq_module import STAMP, incoming

    env = qq_env
    scope = ReplyScope("qq", "10001", "private", "10002")
    env.provider.unknown = True
    env.module.accept({**incoming(1), "time": STAMP})
    event = (await env.module.runtime_poll())[0]
    assert (await env.core.handle(event))["action"]["status"] == "uncertain"
    assert len(env.provider.sent) == 1

    # A human revalidated the native channel, then the administrator stopped the scope.
    env.module.tainted = False
    exclusion(env.store, scope, excluded=True, at=STAMP + 1)
    exclusion(env.store, scope, excluded=False, at=STAMP + 2)
    assert await env.module.runtime_poll() == []
    assert len(env.provider.sent) == 1


# --- WeChat intake, admission and final submission ----------------------------


@pytest.mark.asyncio
async def test_wechat_private_exclusion_never_rearms_old_input_and_accepts_new_messages(pg_store):
    from tests_postgres.test_wechat_module import compose
    from tests_postgres.test_wechat_role import enqueue
    from tests_wechat.test_receiver import STAMP, row

    env = await compose(pg_store)
    scope = wechat_scope(env)
    try:
        env.core.start()
        enqueue(env, row(1))  # admitted under the pre-exclusion permit
        admitted = await env.adapter.collect_once()
        assert len(admitted) == 1

        exclusion(pg_store, scope, excluded=True, at=STAMP - 10)
        enqueue(env, row(2))  # received while the exclusion is active
        assert await env.adapter.collect_once() == []

        exclusion(pg_store, scope, excluded=False, at=STAMP + 5.5)  # eligibility starts at STAMP + 6
        # The old admission is retired rather than re-armed, and its permit is dead.
        assert env.session.admitted_pending() == []
        with pytest.raises(AssistantError):
            await env.core.handle(admitted[0])
        assert env.driver.sent == []

        enqueue(env, row(6))  # timestamped at the first accepted second
        incoming = await env.adapter.collect_once()
        assert len(incoming) == 1
        assert (await env.core.handle(incoming[0]))["action"]["status"] == "succeeded"
        assert len(env.driver.sent) == 1
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
async def test_wechat_group_only_answers_messages_after_the_lift(pg_store):
    from tests_postgres.test_wechat_module import compose
    from tests_postgres.test_wechat_role import enqueue
    from tests_wechat.test_receiver import STAMP, row

    env = await compose(pg_store, family=True)
    scope = wechat_scope(env, "family")
    exclusion(pg_store, scope, excluded=True, at=STAMP - 10)
    exclusion(pg_store, scope, excluded=False, at=STAMP + 1.5)  # eligibility starts at STAMP + 2
    try:
        env.core.start()
        enqueue(env, row(1, group=True) | {"trigger": "mention"})
        assert await env.adapter.collect_once() == []
        enqueue(env, row(3, group=True) | {"trigger": "mention"})
        incoming = await env.adapter.collect_once()
        assert len(incoming) == 1
        assert (await env.core.handle(incoming[0]))["action"]["status"] == "succeeded"
        assert len(env.driver.sent) == 1
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.asyncio
async def test_wechat_exclusion_during_prepare_prevents_the_native_send(pg_store):
    from tests_postgres.test_wechat_module import compose
    from tests_postgres.test_wechat_role import enqueue
    from tests_wechat.test_receiver import STAMP, row

    env = await compose(pg_store)
    scope = wechat_scope(env)
    try:
        env.core.start()
        enqueue(env, row(1))
        incoming = (await env.adapter.collect_once())[0]
        env.driver.before = lambda: exclusion(pg_store, scope, excluded=True, at=STAMP + 1)
        result = await env.core.handle(incoming)
        assert result["action"]["status"] == "cancelled"
        assert env.driver.sent == []
    finally:
        env.core.stop()
        await env.adapter.close()


@pytest.mark.parametrize("group", [False, True], ids=["private", "group"])
async def test_wechat_follow_resumes_only_new_messages_after_a_lift(pg_store, tmp_path, group):
    """Real synthetic source -> FollowInbox -> session -> sender, private and group.

    The follow position stores the raw membership tenure while the reply permit is
    wrapped by the exclusion revision, so the two must not be compared to each other.
    """
    import sqlite3
    from contextlib import closing, nullcontext
    from datetime import datetime, timezone

    from companion_wechat.db_probe import message_table
    from tests_postgres.test_wechat_module import compose
    from tests_wechat.test_db_source import GROUP, OWNER, SELF, SINCE, Harness

    lab = Harness(tmp_path)
    target, kind = (GROUP, "group") if group else (OWNER, "private")
    lab.targets = [item for item in lab.targets if item.username != target]
    conn = lab.shards["message_0.db"]
    table = message_table(target)
    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN source')

    def insert(number, created):
        conn.execute(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?,?)',
                     (number, 1000 + number, created, 1, 3 if group else 1, f"synthetic {number}", 9,
                      f"<msgsource><atuserlist>{SELF}</atuserlist></msgsource>"))

    with closing(sqlite3.connect(":memory:", isolation_level=None)) as contacts, lab.session() as parent:
        contacts.execute("CREATE TABLE contact(username,local_type,flag,delete_flag,verify_flag,is_in_chat_room)")
        contacts.execute("INSERT INTO contact VALUES (?,?,1,0,0,?)", (target, 2 if group else 1, int(group)))
        parent._prepared.open_contacts = lambda: nullcontext(contacts)
        parent._prepared.shards = {lab.account / "db_storage/message/message_0.db": conn}
        env = await compose(pg_store, previous=parent.snapshot(), authorized=False, family=True,
                           clock=lambda: datetime.fromtimestamp(SINCE + 100, timezone.utc))
        receiver = env.receiver
        receiver.fixed_windows = receiver.monitor_members = True
        category = Audience("wechat", receiver.bindings.identity, kind)
        policy = set_mode(pg_store, category, True)
        pg_store.set_state(category.key, {**policy, "since": SINCE})
        env.driver.members.add(target)
        scope = ReplyScope("wechat", receiver.bindings.identity, kind, target)

        async def request(command, **args):
            if command == "chat_member_page":
                return parent.member_page(**args, find_chats=True)
            if command == "member_window":
                args["limit"] = 1
            return getattr(parent, command)(**args)

        env.worker.request = request
        tick = [0.0]
        receiver.clock = lambda: tick[0]

        async def poll():
            tick[0] += 6
            return await env.adapter.collect_once()

        async def pending():
            for _ in range(8):
                found = await poll()
                if found:
                    assert len(found) == 1
                    return found[0]
            pytest.fail("new messages never became eligible after lifting the exclusion")

        try:
            env.core.start()
            insert(1, SINCE)
            initial = await pending()
            assert (await env.core.handle(initial))["action"]["status"] == "succeeded"
            exclusion(pg_store, scope, excluded=True, at=SINCE + 1)
            insert(2, SINCE + 2)
            for _ in range(3):
                assert await poll() == []
            exclusion(pg_store, scope, excluded=False, at=SINCE + 3)
            insert(3, SINCE + 4)
            fresh = await pending()
            # The excluded-period message is not replayed as part of the new reply.
            assert len(env.session.merged.for_event(fresh)["members"]) == 1
            assert (await env.core.handle(fresh))["action"]["status"] == "succeeded"
            assert len(env.driver.sent) == 2
            for _ in range(3):
                assert await poll() == []
        finally:
            env.core.stop()
            await env.adapter.close()
    lab.close()


# --- QQ owned native provider and receiver -----------------------------------


def _private_bound(seq, time):
    return {"accountUin": "10001", "accountUid": "u_self", "peerUin": "10002", "peerUid": "u_peer",
            "senderUin": "10002", "senderUid": "u_peer", "clientSeq": str(5000 + seq),
            "time": str(time), "random": str(100 + seq)}


def _group_bound(seq, time):
    return {"account": "10001", "group": "10003", "seq": str(seq), "senderUin": "10002",
            "senderUid": "u_peer", "time": str(time)}


class OwnedWorker:
    """Synthetic owned native worker for one private and one group conversation."""

    def __init__(self):
        from copy import deepcopy

        from tests.test_qq_native_normalization import IDENTITY

        self.closed, self.submits, self.calls = False, 0, []
        self.identity = deepcopy(IDENTITY)
        self.identity["tenures"] = {"private": {"10002": 3}, "group": {"10003": 3}}
        self.hook = lambda command, args: None
        self.state = {kind: {"upper": 1, "time": STAMP, "messages": []} for kind in ("private", "group")}

    def open(self):
        assert not self.closed
        return self.identity

    def close(self):
        self.closed = True

    def arrive(self, kind, payload, *, seq, time):
        self.state[kind]["messages"].append(payload)
        self.state[kind]["upper"] = seq
        self.state[kind]["time"] = time

    def _bound(self, kind, seq, time):
        return _private_bound(seq, time) if kind == "private" else _group_bound(seq, time)

    def _inside(self, kind, message, args):
        if kind == "group":
            return int(args["lower"]["seq"]) < int(message["msgSeq"]) <= int(args["upper"]["seq"])
        return (int(message["msgTime"]) >= int(args["lower"]["time"])
                and int(message["msgTime"]) <= int(args["upper"]["time"]))

    def request(self, command, **args):
        from copy import deepcopy

        assert not self.closed
        self.calls.append((command, deepcopy(args)))
        self.hook(command, args)
        if command == "identity":
            return deepcopy(self.identity)
        if command == "prepare_text":
            self.prepared = args.get("kind")
            return {"token": "a" * 64, "message_id": "9001"}
        if command == "submit_text":
            self.submits += 1
            # A private reply carries no group sequence reference.
            reference = None if self.prepared == "private" else "native-seq:5"
            return {"message_id": "9001", "reference": reference}
        kind, _, action = command.partition("_")
        state = self.state[kind]
        if action == "head":
            return self._bound(kind, state["upper"], state["time"])
        if action == "open":
            from companion_core.contracts import stable_key

            self.lower, self.upper = deepcopy(args["lower"]), deepcopy(args["upper"])
            self.frozen = [item for item in state["messages"] if self._inside(kind, item, args)]
            self.window = stable_key("synthetic-range", kind, self.lower, self.upper)
            return {"window_id": self.window, "upper": self.upper, "count": len(self.frozen),
                    "history_status": {"status": "complete", "reasons": []}}
        assert action == "page", command
        return {"window_id": self.window, "kind": kind,
                "target": "10002" if kind == "private" else "10003",
                "lower": self.lower, "upper": self.upper, "page": args["page"],
                "complete": True,
                "messages": deepcopy(self.frozen) if args["page"] == 0 else [],
                "history_status": {"status": "complete", "reasons": []}}


async def _owned_native(pg_store, tmp_path, *, follow):
    from datetime import datetime, timezone

    from companion_core.runtime import Companion
    from companion_qq.native_provider import NativeProvider
    from tests_postgres.test_qq_module import OPTIONS
    from tests_postgres.test_wechat_role import Host, Model

    workers = []
    native = {"account": {"uin": "10001", "uid": "u_self"}, "private_targets": ["10002"],
              "groups": ["10003"]}

    def factory(config):
        worker = OwnedWorker()
        workers.append(worker)
        return worker

    module = QQModule(pg_store, tmp_path, options=OPTIONS,
                      factory=lambda: NativeProvider(pg_store, native, worker_factory=factory,
                                                     follow_settings=follow))
    module.probe()
    core = Companion(pg_store, Model(), Host(), [module],
                     clock=lambda: datetime.fromtimestamp(STAMP, timezone.utc), run_key="core-run:qq")
    core.start()
    return module, core, workers


async def _reply(module, core, workers, kind, payload, *, seq, time, limit=10):
    """Add one native arrival and poll until the one reply is sent."""
    workers[-1].arrive(kind, payload, seq=seq, time=time)
    for _ in range(limit):
        found = await module.runtime_poll()
        if found:
            assert len(found) == 1
            assert (await core.handle(found[0]))["action"]["status"] == "succeeded"
            return
    pytest.fail("no reply was scheduled for the " + kind + " conversation")


async def test_qq_native_manual_stop_keeps_the_account_and_other_conversations_running(pg_store, tmp_path):
    from tests.test_qq_native_normalization import raw
    from tests_postgres.test_qq_native_windows import message

    module, core, workers = await _owned_native(pg_store, tmp_path, follow=False)
    private = ReplyScope("qq", "10001", "private", "10002")
    try:
        for target in module.reply_targets():
            ReplyAuthorizations(pg_store).update(target.scope, enabled=True, expected_version=None, actor="test")
        assert await module.runtime_poll() == []  # First registration of both conversations.
        assert await module.runtime_poll() == []
        await _reply(module, core, workers, "private", {**raw(2), "msgTime": str(STAMP)},
                     seq=2, time=STAMP)
        assert workers[-1].submits == 1

        # Stop one conversation: the owned provider must keep working.
        exclusion(pg_store, private, excluded=True, at=STAMP + 1)
        await _reply(module, core, workers, "group",
                     {**message(2, mention=True), "msgTime": str(STAMP + 5)},
                     seq=2, time=STAMP + 5)
        assert workers[-1].submits == 2 and not workers[-1].closed and module.ready

        # A message received while excluded is never answered, then the lift restarts
        # eligibility at the next message only.
        workers[-1].arrive("private", {**raw(3), "msgTime": str(STAMP + 6)}, seq=3, time=STAMP + 6)
        for _ in range(3):
            assert await module.runtime_poll() == []
        exclusion(pg_store, private, excluded=False, at=STAMP + 10)
        await _reply(module, core, workers, "private", {**raw(4), "msgTime": str(STAMP + 12)},
                     seq=4, time=STAMP + 12)
        assert workers[-1].submits == 3 and not workers[-1].closed and module.ready
        assert module.code == ""
        pending = await module.runtime_poll()
        assert pending == []
    finally:
        core.stop()
        await module.close()


async def test_qq_native_allow_all_stop_written_mid_read_stays_local(pg_store, tmp_path, monkeypatch):
    import companion_core.reply_audience as audience_module
    from tests.test_qq_native_normalization import raw
    from tests_postgres.test_qq_native_windows import message

    category = Audience("qq", "10001", "group")
    original = audience_module.time.time
    audience_module.time.time = lambda: STAMP - 10
    try:
        set_mode(pg_store, category, True)
    finally:
        audience_module.time.time = original
    module, core, workers = await _owned_native(pg_store, tmp_path, follow=True)
    private = ReplyScope("qq", "10001", "private", "10002")
    group = ReplyScope("qq", "10001", "group", "10003")
    try:
        ReplyAuthorizations(pg_store).update(private, enabled=True, expected_version=None, actor="test")
        assert await module.runtime_poll() == []
        assert await module.runtime_poll() == []

        # The administrator stops the group while its frozen range is being read.
        def stop(command, args):
            if command == "group_open":
                exclusion(pg_store, group, excluded=True, at=STAMP)

        workers[-1].hook = stop
        workers[-1].arrive("group", {**message(2, mention=True), "msgTime": str(STAMP)},
                           seq=2, time=STAMP)
        for _ in range(4):
            assert await module.runtime_poll() == []
        # The account and the other conversation were never paused or closed.
        assert module.ready and not workers[-1].closed and module.code == ""
        assert module.native_inbox.count() == 0
        workers[-1].hook = lambda command, args: None
        await _reply(module, core, workers, "private", {**raw(2), "msgTime": str(STAMP)},
                     seq=2, time=STAMP)
        assert workers[-1].submits == 1 and module.ready

        # After the lift the old range is re-read: only the newer message replies once.
        exclusion(pg_store, group, excluded=False, at=STAMP + 1)
        await _reply(module, core, workers, "group",
                     {**message(3, mention=True), "msgTime": str(STAMP + 3)}, seq=3, time=STAMP + 3)
        assert workers[-1].submits == 2 and not workers[-1].closed and module.ready
        assert await module.runtime_poll() == []
    finally:
        core.stop()
        await module.close()
