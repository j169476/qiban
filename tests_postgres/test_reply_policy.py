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


def test_permission_or_policy_change_invalidates_old_ticket(env):
    store, policy, private, _group, _now = env
    ticket = policy.admit(private, "one", "direct", "run")
    ReplyPolicies(store).update("qq", "10001", {"private_daily_replies": 201}, None, "test")
    assert not policy.eligible(ticket, "run")
    with pytest.raises(AssistantError) as conflict:
        ReplyPolicies(store).update("qq", "10001", {"private_daily_replies": 100}, None, "test")
    assert conflict.value.code == "edit_conflict"


def test_policy_values_do_not_coerce_booleans_or_accept_unknown_fields(env):
    store, *_ = env
    for changes in ({"private_daily_replies": True}, {"private_daily_replies": -1}, {"unknown": 1}, {}):
        with pytest.raises(AssistantError):
            ReplyPolicies(store).update("qq", "10001", changes, None, "test")
