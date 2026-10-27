from datetime import datetime, timedelta, timezone

import pytest

from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.reply_policy import Participation, ReplyPolicies


@pytest.fixture
def env(pg_store):
    now = [datetime(2026, 10, 25, 10, tzinfo=timezone.utc)]
    policy = Participation(pg_store, clock=lambda: now[0])
    private = ReplyScope("qq", "10001", "private", "10002")
    group = ReplyScope("qq", "10001", "group", "10003")
    for scope in (private, group):
        ReplyAuthorizations(pg_store).update(scope, enabled=True, expected_version=None, actor="test")
    return pg_store, policy, private, group, now


def test_group_wake_reply_cap_silence_end_and_reactivation(env):
    store, policy, _private, group, _now = env
    assert policy.admit(group, "ordinary", "none", "run") is None
    first = policy.admit(group, "mention", "mention", "run")
    for n in range(5):
        ticket = first if n == 0 else policy.admit(group, str(n), "none", "run")
        policy.reserve(ticket, "run", "action" + str(n))
        policy.settle("action" + str(n), "succeeded")
    assert policy.admit(group, "six", "none", "run") is None
    again = policy.admit(group, "new-wake", "reply", "run")
    assert again["window"] != first["window"]
    with pytest.raises(AssistantError):
        policy.check(first, "run")
    policy.end(again, "run")
    assert policy.admit(group, "after-end", "none", "run") is None
    assert policy.admit(group, "after-end-wake", "mention", "run")


def test_private_has_separate_daily_quota_and_uncertain_reservations_survive_restart(env):
    store, policy, private, group, now = env
    ReplyPolicies(store).update("qq", "10001", {"private_daily_replies": 2}, None, "test")
    ticket = policy.admit(private, "p1", "direct", "run")
    policy.reserve(ticket, "run", "p1-action")
    policy.settle("p1-action", "cancelled")
    assert policy.usage(private)[1] == {"confirmed": 0, "reserved": 0, "uncertain": 0}
    policy.reserve(ticket, "run", "p1-action-retry")
    policy.settle("p1-action-retry", "succeeded")
    policy.settle("p1-action-retry", "succeeded")  # Exactly once accounting.
    second = policy.admit(private, "p2", "direct", "run")
    policy.reserve(second, "run", "p2-action")
    policy.settle("p2-action", "uncertain")
    restarted = Participation(store, clock=lambda: now[0])
    assert restarted.admit(private, "p3", "direct", "new-run") is None
    assert restarted.admit(group, "g1", "mention", "new-run") is not None
    now[0] += timedelta(days=1)
    assert restarted.admit(private, "p3", "direct", "new-run") is not None


def test_windows_expire_do_not_reset_on_repeated_mentions_and_obey_decision_cap(env):
    store, policy, _private, group, now = env
    ReplyPolicies(store).update("qq", "10001", {"group_session_decisions": 2}, None, "test")
    first = policy.admit(group, "one", "mention", "run")
    second = policy.admit(group, "two", "mention", "run")
    assert first["window"] == second["window"]
    assert policy.admit(group, "three", "none", "run") is None
    fresh = policy.admit(group, "four", "mention", "run")
    now[0] += timedelta(minutes=11)
    assert not policy.eligible(fresh, "run")
    assert policy.admit(group, "five", "none", "run") is None
    renewed = policy.admit(group, "six", "mention", "new-run")
    assert renewed and not policy.eligible(renewed, "old-run")


def test_quota_change_keeps_ticket_but_checks_live_limit_and_permission(env):
    store, policy, private, _group, _now = env
    ticket = policy.admit(private, "one", "direct", "run")
    ReplyPolicies(store).update("qq", "10001", {"private_daily_replies": 201}, None, "test")
    assert policy.eligible(ticket, "run")
    with pytest.raises(AssistantError) as conflict:
        ReplyPolicies(store).update("qq", "10001", {"private_daily_replies": 100}, None, "test")
    assert conflict.value.code == "edit_conflict"


def test_policy_values_do_not_coerce_booleans_or_accept_unknown_fields(env):
    store, *_ = env
    for changes in ({"private_daily_replies": True}, {"private_daily_replies": -1}, {"unknown": 1}, {}):
        with pytest.raises(AssistantError):
            ReplyPolicies(store).update("qq", "10001", changes, None, "test")


def test_contact_overrides_restore_defaults_and_timezone_midnight(env):
    store, participation, first, _group, now = env
    second = ReplyScope("qq", "10001", "private", "10004")
    ReplyAuthorizations(store).update(second, enabled=True, expected_version=None, actor="test")
    policies = participation.policies
    policies.update("qq", "10001", {"private_daily_replies": 1}, None, "test")
    ticket = participation.admit(first, "one", "direct", "run")
    participation.reserve(ticket, "run", "one")
    assert participation.admit(first, "two", "direct", "run") is None
    assert participation.admit(second, "two", "direct", "run")
    current = policies.effective(first)
    policies.update_object(first, {"private_daily_replies": 3}, current["version"], "test")
    assert participation.admit(first, "two", "direct", "run")
    base = policies.get("qq", "10001")
    policies.update("qq", "10001", {"private_daily_replies": 2}, base["version"], "test")
    assert policies.effective(first)["settings"]["private_daily_replies"] == 3
    assert policies.effective(second)["settings"]["private_daily_replies"] == 2
    current = policies.effective(first)
    policies.update_object(first, {}, current["version"], "test", restore=True)
    assert policies.effective(first)["inherited"]
    assert participation.usage(first)[1]["reserved"] == 1
    old_key = participation.usage(first)[0]
    now[0] = datetime(2026, 10, 25, 16, tzinfo=timezone.utc)  # Asia/Shanghai midnight.
    assert participation.usage(first)[0] != old_key
    participation.settle("one", "succeeded")
    assert store.state(old_key)["confirmed"] == 1
    assert participation.usage(first)[1]["confirmed"] == 0
    assert policies.effective(first)["settings"]["private_daily_replies"] == 2


def test_group_changes_keep_started_usage_and_do_not_revive_ended_stage(env):
    store, participation, _private, group, now = env
    policies = participation.policies
    ticket = participation.admit(group, "one", "mention", "run")
    participation.reserve(ticket, "run", "one")
    participation.settle("one", "succeeded")
    original = store.state(participation.window_key(group))
    now[0] += timedelta(minutes=2)
    current = policies.effective(group)
    policies.update_object(group, {"group_session_replies": 8, "group_session_minutes": 5}, current["version"], "test")
    changed = store.state(participation.window_key(group))
    assert changed["id"] == original["id"] and changed["started"] == original["started"]
    assert changed["expires"] == original["started"] + 300 and changed["confirmed"] == 1
    current = policies.effective(group)
    policies.update_object(group, {"group_session_minutes": 1}, current["version"], "test")
    assert not participation.eligible(ticket, "run")
    current = policies.effective(group)
    policies.update_object(group, {}, current["version"], "test", restore=True)
    assert participation.admit(group, "quiet", "none", "run") is None
    assert participation.admit(group, "wake", "mention", "run")["window"] != original["id"]


def test_lower_limit_blocks_prepared_send_without_changing_usage_or_other_contacts(env):
    store, participation, private, _group, _now = env
    ticket = participation.admit(private, "one", "direct", "run")
    policy = participation.policies.effective(private)
    participation.policies.update_object(private, {"private_daily_replies": 0}, policy["version"], "test")
    with pytest.raises(AssistantError) as error:
        participation.reserve(ticket, "run", "one")
    assert error.value.code == "reply_quota_exhausted"
    assert participation.spent(participation.usage(private)[1]) == 0


@pytest.mark.parametrize("platform", ["qq", "wechat"])
def test_legacy_bucket_migration_preserves_each_recipient_and_inflight_settlement(pg_store, platform):
    from companion_core.contracts import stable_key

    now = datetime(2026, 10, 25, 10, tzinfo=timezone.utc)
    participation = Participation(pg_store, clock=lambda: now)
    first = ReplyScope(platform, "account", "private", "first")
    second = ReplyScope(platform, "account", "private", "second")
    legacy = stable_key("private-reply-usage", platform, "account", "2026-10-25")
    pg_store.set_state(legacy, {"confirmed": 1, "reserved": 1, "uncertain": 0})
    with pg_store.db() as db:
        for identifier, scope, status in [("a", first, "succeeded"), ("b", second, "reserved")]:
            event = {"platform": platform, "visibility": "private", "metadata": {
                platform: {"target_id" if platform == "qq" else "target_ref": scope.target_id}}}
            db.execute("INSERT INTO core_actions VALUES(?,?,?,?,?,?,?,?,?,?)", (
                identifier, identifier, "run", platform, "internal-account", "message.reply",
                pg_store.ledger_encode("core_actions.body", {"event": event}), "dispatching", None, now.isoformat()))
            pg_store.set_state("reply-send:" + identifier, {"bucket": legacy, "window": None, "status": status}, db)
    assert participation.usage(first)[1] == {"confirmed": 1, "reserved": 0, "uncertain": 0}
    assert participation.usage(second)[1] == {"confirmed": 0, "reserved": 1, "uncertain": 0}
    participation.settle("b", "uncertain")
    assert participation.usage(second)[1] == {"confirmed": 0, "reserved": 0, "uncertain": 1}
    assert participation.usage(first)[1]["confirmed"] == 1  # Migration is idempotent.


def test_legacy_inconsistent_ledger_never_silently_resets_usage(env):
    from companion_core.contracts import stable_key

    store, participation, private, _group, _now = env
    legacy = stable_key("private-reply-usage", "qq", "10001", "2026-10-25")
    store.set_state(legacy, {"confirmed": 7, "reserved": 0, "uncertain": 0})
    with pytest.raises(AssistantError) as error:
        participation.usage(private)
    assert error.value.code == "reply_quota_migration_required"
    assert store.state(legacy)["confirmed"] == 7
