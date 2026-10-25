import asyncio
from dataclasses import replace

import pytest

from companion_wechat.bindings import ConversationBinding
from companion_wechat.contracts import SendRequest
from companion_wechat.gateway import PermitAuthority
from companion_wechat.providers.fake import FakeGateway


@pytest.fixture
def gateway():
    now = [100.0]
    authority = PermitAuthority(b"synthetic-only-key" * 3, clock=lambda: now[0])
    value = FakeGateway("synthetic-self", authority)
    value.bind_target(ConversationBinding("dm", "peer-target", "private", "peer"))
    request = SendRequest("action-1", "generation-1", "synthetic-self", "synthetic-epoch",
                          "dm", 1, "peer-target", "synthetic message")
    return value, authority, request, now


@pytest.mark.asyncio
@pytest.mark.parametrize("flaw", ["expired", "signature", "digest", "ticket", "foreign-authority"])
async def test_invalid_permit_never_submits(gateway, flaw):
    bridge, authority, request, now = gateway
    prepared = await bridge.prepare(request)
    permit = authority.issue(prepared, request)
    if flaw == "expired":
        now[0] = 103
    elif flaw == "signature":
        permit = replace(permit, signature="0" * 64)
    elif flaw == "digest":
        permit = replace(permit, request_digest="0" * 64)
    elif flaw == "ticket":
        permit = replace(permit, ticket="different-ticket")
    else:
        permit = PermitAuthority(b"wrong-authority" * 4, clock=lambda: now[0]).issue(prepared, request)
    result = await bridge.commit(permit, guard=lambda: None)
    assert result.status == "cancelled" and not bridge.sent


@pytest.mark.asyncio
async def test_concurrent_duplicate_permit_is_consumed_once(gateway):
    bridge, authority, request, _ = gateway
    permit = authority.issue(await bridge.prepare(request), request)
    outcomes = await asyncio.gather(*(bridge.commit(permit, guard=lambda: None) for _ in range(5)))
    assert [r.status for r in outcomes].count("succeeded") == 5
    assert len(bridge.sent) == 1


@pytest.mark.asyncio
async def test_reprepared_same_action_is_not_sent_twice(gateway):
    bridge, authority, request, _ = gateway
    for _ in range(2):
        permit = authority.issue(await bridge.prepare(request), request)
        assert (await bridge.commit(permit, guard=lambda: None)).status == "succeeded"
    assert len(bridge.sent) == 1


@pytest.mark.asyncio
async def test_explicit_provider_rejection_has_no_send_side_effect(gateway):
    bridge, authority, request, _ = gateway
    bridge.outcome = "rejected"
    permit = authority.issue(await bridge.prepare(request), request)
    assert (await bridge.commit(permit, guard=lambda: None)).status == "failed"
    assert not bridge.sent


@pytest.mark.asyncio
async def test_gateway_cannot_change_prepared_text_after_permit(gateway):
    bridge, authority, request, _ = gateway
    prepared = await bridge.prepare(request)
    permit = authority.issue(prepared, request)
    bridge.pending[prepared.ticket] = replace(request, text="different message")
    assert (await bridge.commit(permit, guard=lambda: None)).status == "cancelled"
    assert not bridge.sent
