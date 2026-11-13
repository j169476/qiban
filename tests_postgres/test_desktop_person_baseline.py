"""One relationship body; existing identity graph and real scoped actors only."""
import json
import uuid
from datetime import datetime, timezone

import pytest

from companion_core import baseline
from companion_core.contracts import ActorRef, Event, stable_key
from companion_core.identity import IdentityService
from companion_core.person_baseline import SELECTION, matches
from companion_core.relations import RelationService
from companion_core.runtime import Companion
from tests_postgres.test_desktop_management import call, desktop_store, service
from tests_postgres.test_runtime import Adapter, Host, Model

__all__ = ["desktop_store", "service"]
QQ = ActorRef("qq", "123", "bot")
WX = ActorRef("wechat", "test-wx", "wx-bot")
BILI = ActorRef("bilibili", "123")
OTHER = ActorRef("qq", "123", "different-bot")
SETTING = {"owner_name": "测试人物", "addresses": ["朋友🙂"], "level": "熟悉", "description": "清楚地说明数字123"}


def seed(service):
    graph = RelationService(service.repository, service.authority)
    for actor in (QQ, WX, BILI, OTHER):
        graph.register_actor(actor)
    for second in (WX, BILI):
        key = stable_key("link", *sorted((QQ.key, second.key)))
        service.store.write("identity_links", {"schema_version": 1, "id": key, "left": QQ.as_dict(),
            "right": second.as_dict(), "status": "hard", "manual": True, "locked": True,
            "reason": "synthetic existing link", "evidence": [], "history": []}, None, "fixture", key)
    baseline.publish(service.store, baseline.preview(service.store), "fixture")
    return {"anchor": QQ.key, "selected": [QQ.key, WX.key],
            "expected": service.store.read("persona")["version"], "setting": SETTING}


def test_one_body_explicit_subset_scoped_and_receipted(service):
    args = seed(service)
    group = call(service, "relationship.person", {"anchor": QQ.key})["result"]["members"]
    assert {r["key"] for r in group} == {QQ.key, WX.key, BILI.key}
    key = uuid.uuid4().hex
    result = call(service, "relationship.save", args, key)
    assert result["ok"] and call(service, "relationship.save", args, key) == result
    assert [matches(service.store, a) for a in (QQ, WX, BILI, OTHER)] == [True, True, False, False]
    data = call(service, "relationship.read")["result"]
    assert data["setting"]["addresses"] == ["朋友🙂"]
    assert "owner_uid" not in data["setting"]
    assert not any(x.key in json.dumps(data["setting"]) for x in (QQ, WX, BILI))
    assert call(service, "relationship.save", args)["error"] == "edit_conflict"
    assert call(service, "relationship.clear", {"expected": data["expected"]})["ok"]
    assert call(service, "relationship.read")["result"]["setting"] is None
    assert len(IdentityService(service.store).person(QQ)["members"]) == 3


def test_unrelated_selection_and_invalid_form_rollback(service):
    args = seed(service)
    assert call(service, "relationship.save", {**args, "selected": [QQ.key, OTHER.key]})["error"] == "identity_changed"
    assert not call(service, "relationship.save", {**args, "setting": {**SETTING, "addresses": []}})["ok"]
    assert service.store.state(SELECTION) is None
    assert service.store.read("persona")["version"] == args["expected"]
    assert len(IdentityService(service.store).person(OTHER)["members"]) == 1


def test_revoked_link_no_longer_matches_and_lock_denies(service):
    args = seed(service)
    assert call(service, "relationship.save", args)["ok"]
    ref = stable_key("link", *sorted((QQ.key, WX.key)))
    IdentityService(service.store).revoke(ref, service.store.read("identity_links", ref)["version"], "synthetic withdrawal")
    assert not matches(service.store, WX)
    assert matches(service.store, QQ)
    service.enable_lock("Synthetic-Relationship-Lock-2026!")
    service.lock()
    for op, params in (("read", {}), ("accounts", {"after": ""}), ("person", {"anchor": QQ.key}), ("save", args)):
        assert call(service, "relationship."+op, params)["error"] == "desktop_locked"


@pytest.mark.asyncio
@pytest.mark.parametrize(("sender","expected_match"), [(QQ,True),(WX,True),(OTHER,False)])
async def test_governed_reply_only_receives_current_sender_match(service,sender,expected_match):
    args = seed(service)
    assert call(service,"relationship.save",args)["ok"]
    class ScopedAdapter(Adapter):
        platform, account = sender.platform, sender.scope
    model, adapter = Model(), ScopedAdapter()
    core = Companion(service.store, model, Host(), [adapter])
    core.start()
    try:
        event = Event(sender.platform,sender.scope,"test",sender,"private",datetime.now(timezone.utc).isoformat(),"草图")
        await core.handle(event)
        ctx = next(c for c in model.contexts if "event" in c)
        assert ctx["event"]["relationship_context"]["is_owner"] is expected_match
        assert ctx["persona"]["relationship_baseline"]["addresses"] == ["朋友🙂"]
        text = json.dumps(ctx,ensure_ascii=False)
        assert BILI.key not in text
        if sender != WX:
            assert WX.user_id not in text and WX.key not in text
        else:
            assert QQ.key not in text
        assert not ctx["person_profile"]
    finally:
        core.stop()

@pytest.mark.asyncio
async def test_revocation_after_generation_blocks_delivery(service):
    args = seed(service)
    assert call(service, "relationship.save", args)["ok"]
    class SenderAdapter(Adapter):
        platform, account = WX.platform, WX.scope
    adapter = SenderAdapter()
    class RevokingModel(Model):
        async def generate(self, instructions, context, schema, images=(), **options):
            result = await super().generate(instructions, context, schema, images, **options)
            if "event" in context:
                assert context["event"]["relationship_context"]["is_owner"]
                ref = stable_key("link", *sorted((QQ.key, WX.key)))
                IdentityService(service.store).revoke(ref, service.store.read("identity_links",ref)["version"],"fixture")
            return result
    from companion_core.errors import AssistantError
    core = Companion(service.store, RevokingModel(), Host(), [adapter])
    core.start()
    try:
        event=Event(WX.platform,WX.scope,"changed",WX,"private",datetime.now(timezone.utc).isoformat(),"测试")
        with pytest.raises(AssistantError) as exc:
            await core.handle(event)
        assert exc.value.code=="context_changed"
        assert adapter.sent==[]
    finally:
        core.stop()
