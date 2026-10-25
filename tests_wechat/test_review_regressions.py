"""Independent regressions found while reviewing P1's trust boundaries."""
from dataclasses import replace

import pytest
from test_connector import dispatch, incoming
from test_connector import env as env

from companion_core import events
from companion_wechat.contracts import Connection, SendRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "binding", "policy"])
async def test_final_gateway_submission_rechecks_service_state_after_queueing(env, change):
    event = await incoming(env)

    def revoke_after_permit():
        if change == "source":
            events.forget(env.store, event.id)
        elif change == "binding":
            env.bindings.bind(replace(env.private, enabled=False), expected_revision=1)
        else:
            policy = env.store.read("policy")
            policy["data"]["daily_model_cycles"] += 1
            env.store.write("policy", policy["data"], policy["version"], "test-admin")

    env.gateway.before_commit = revoke_after_permit
    result = await dispatch(env, event)
    assert result["status"] == "cancelled"
    assert not env.gateway.sent


@pytest.mark.asyncio
async def test_replaying_consumed_send_ticket_never_claims_previous_send_was_cancelled(env):
    request = SendRequest("synthetic-attempt", "synthetic-generation", "synthetic-self", "synthetic-epoch",
                          env.private.key, env.private.revision, env.private.target_ref, "synthetic reply")
    prepared = await env.gateway.prepare(request)
    permit = env.permits.issue(prepared, request)
    first = await env.gateway.commit(permit, guard=lambda: None)
    second = await env.gateway.commit(permit, guard=lambda: None)
    assert first.status == "succeeded"
    assert second.status in {"succeeded", "uncertain"}
    assert len(env.gateway.sent) == 1


@pytest.mark.asyncio
async def test_completed_action_id_cannot_be_reused_for_different_text(env):
    request = SendRequest("synthetic-attempt", "synthetic-generation", "synthetic-self", "synthetic-epoch",
                          env.private.key, env.private.revision, env.private.target_ref, "first reply")
    prepared = await env.gateway.prepare(request)
    permit = env.permits.issue(prepared, request)
    assert (await env.gateway.commit(permit, guard=lambda: None)).status == "succeeded"

    conflicting = replace(request, text="different reply")
    prepared = await env.gateway.prepare(conflicting)
    permit = env.permits.issue(prepared, conflicting)
    result = await env.gateway.commit(permit, guard=lambda: None)
    assert result.status != "succeeded"
    assert len(env.gateway.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["binding_revision", "connection_epoch"])
async def test_reserved_replay_preserves_source_but_cannot_send_under_new_binding(env, change):
    event = await incoming(env)
    original = events.get(env.store, event.id)
    if change == "binding_revision":
        binding = env.bindings.bind(env.private, expected_revision=1)
        env.gateway.bind_target(binding)
    else:
        env.gateway.session = Connection("synthetic-self", "new-synthetic-epoch")

    replay, _ = await env.connector.receive({"since": "2026-10-24T04:00:00+00:00"})
    assert replay == [event]
    assert events.get(env.store, event.id) == original
    assert (await dispatch(env, event))["status"] == "cancelled"
    assert not env.gateway.sent


@pytest.mark.asyncio
async def test_permit_expiring_during_final_service_check_cannot_submit(env):
    now = [100.0]
    env.permits.clock = lambda: now[0]
    request = SendRequest("synthetic-attempt", "synthetic-generation", "synthetic-self", "synthetic-epoch",
                          env.private.key, env.private.revision, env.private.target_ref, "synthetic reply")
    prepared = await env.gateway.prepare(request)
    permit = env.permits.issue(prepared, request)

    def slow_final_check():
        now[0] += 3.0

    result = await env.gateway.commit(permit, guard=slow_final_check)
    assert result.status == "cancelled"
    assert not env.gateway.sent
