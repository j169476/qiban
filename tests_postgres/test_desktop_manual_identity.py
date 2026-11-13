"""Both identity entry points use one graph and preserve account-specific disclosure."""
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from companion_core import events
from companion_core.contracts import ActorRef, Event, stable_key
from companion_core.errors import AssistantError
from companion_core.identity import IdentityService
from companion_core.person_baseline import matches
from companion_core.runtime import Companion
from tests_postgres.test_desktop_management import call, desktop_store, service
from tests_postgres.test_runtime import Adapter, Host, Model

__all__ = ["desktop_store", "service"]
QQ = ActorRef("qq", "123", "bot")
WX = ActorRef("wechat", "wx-test", "wx-bot")
BILI = ActorRef("bilibili", "2468")


def proof(store, *actors):
    messages = []
    for i, actor in enumerate(actors):
        text = ("我的微信是wx-test，我们约在周五看蓝色草图。" if i == 0 else "我们约在周五一起看蓝色草图。")
        event = Event(actor.platform, actor.scope or "bot", str(i), actor, "dm", datetime.now(timezone.utc).isoformat(), text)
        record = events.ingest(store, event)
        messages.append((event, {"event_id": event.id, "version": record["version"], "quote": text}))
    return messages


def proposal(refs, kind="claim"):
    return {"kind": kind, "reason": "双方提到了同一份蓝色草图约定", "specific_correspondence": kind == "correspondence",
            "left": QQ.as_dict(), "right": WX.as_dict(), "evidence": refs}


def pair(a, b):
    return {"left": a.key, "right": b.key}


def confirm(service, a, b):
    args = pair(a,b)
    result = call(service, "relationship.link_preview", args)["result"]
    return call(service, "relationship.link_confirm", {**args, "expected": result["expected"]})


def test_manual_confirmation_keeps_evidence_and_separate_selection(service):
    (_, first), _ = proof(service.store, QQ, WX)
    graph = IdentityService(service.store)
    old = graph.apply(proposal([first]))
    args = pair(QQ, WX)
    preview = call(service, "relationship.link_preview", args)["result"]
    assert preview["groups"] == [[QQ.as_dict()], [WX.as_dict()]]
    assert graph.person(QQ)["members"] == [QQ.as_dict()]
    params = {**args, "expected": preview["expected"]}
    key = uuid.uuid4().hex
    result = call(service, "relationship.link_confirm", params, key)
    assert result["ok"] and call(service, "relationship.link_confirm", params, key) == result
    doc = service.store.read("identity_links", old["data"]["id"])["data"]
    assert doc["manual"] and doc["locked"] and doc["status"] == "hard" and doc["evidence"] == [first]
    assert [item["kind"] for item in doc["history"]] == ["claim", "manual_confirmation"]
    assert not matches(service.store, QQ) and not matches(service.store, WX)
    assert not graph.person(QQ)["grants_permissions"]
    assert graph.apply_model([proposal([first])], allowed_evidence=[first["event_id"]]) == []
    service.enable_lock("Synthetic-Manual-Identity-Lock!")
    service.lock()
    assert call(service, "relationship.link_confirm", params, key)["error"] == "desktop_locked"


def test_stale_groups_and_revoked_transitive_link(service):
    proof(service.store, QQ, WX, BILI)
    params = pair(QQ, WX)
    version = call(service, "relationship.link_preview", params)["result"]["expected"]
    assert confirm(service, QQ, BILI)["ok"]
    assert call(service, "relationship.link_confirm", {**params, "expected":version})["error"] == "identity_changed"
    assert confirm(service, QQ, WX)["ok"]
    graph = IdentityService(service.store)
    key = stable_key("link", *sorted((QQ.key, WX.key)))
    graph.revoke(key, service.store.read("identity_links", key)["version"], "synthetic withdrawal")
    assert call(service, "relationship.link_preview", params)["error"] == "identity_conflict"
    assert call(service, "relationship.link_preview", pair(WX,BILI))["error"] == "identity_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True])
async def test_automatic_correspondence_requires_live_maintenance_access(service,revoke):
    (a, first),(b, second) = proof(service.store, QQ, WX)
    # A paraphrase without a matching local clue still needs maintenance access.
    b = replace(b, text="上回提过的画稿，我第五天带过来。")
    second = {"event_id":b.id,"version":events.ingest(service.store,b)["version"],"quote":b.text}
    graph = IdentityService(service.store)
    graph.apply_model([proposal([first])],allowed_evidence={a.id},allowed_kinds={"claim"})
    class Reviewer(Model):
        async def generate(self, instructions, context, schema, images=(), **options):
            self.contexts.append(context)
            assert set(context) == {"candidates", "identity_evidence"}
            assert {item["event"]["id"] for item in context["identity_evidence"]} == {a.id,b.id}
            if revoke:
                service.admin.revoke(service.request(), grant["id"])
            return {"identity_proposals":[proposal([first,second],"correspondence")]}
    model = Reviewer()
    core = Companion(service.store,model,Host(),[Adapter()])
    core.start()
    try:
        await core._identity_review(b)
        assert not model.contexts
        grant = service.admin.authorize(service.request(), grantee=WX.key, owner=QQ.key,
                                        resources=["event/"+a.id], purposes=["maintenance"])
        if revoke:
            with pytest.raises(AssistantError):
                await core._identity_review(b)
            assert len(graph.person(QQ)["members"]) == 1
        else:
            await core._identity_review(b)
            assert len(graph.person(QQ)["members"]) == 2
            assert not graph.links()[0]["data"]["manual"]
        assert len(model.contexts)==1
    finally:
        core.stop()

@pytest.mark.asyncio
@pytest.mark.parametrize("merged", [False, True])
async def test_normal_reply_worker_collects_claim_then_reviews_separately(service,merged):
    (first_event, first),(second_event, second) = proof(service.store, QQ, WX)
    first_event = replace(first_event,text=first_event.text+"体检报告仅自己看。")
    second_event = replace(second_event,text=second_event.text+"今天的银行卡余额属于题外话。")
    first = {"event_id":first_event.id,"version":events.ingest(service.store,first_event)["version"],"quote":"我的微信是wx-test"}
    second = {"event_id":second_event.id,"version":events.ingest(service.store,second_event)["version"],"quote":"我们约在周五一起看蓝色草图。"}
    class QQAdapter(Adapter):
        platform, account = QQ.platform, QQ.scope
    class WXAdapter(Adapter):
        platform, account = WX.platform, WX.scope
    phases = []
    class TwoPlatformModel(Model):
        async def generate(self, instructions, context, schema, images=(), **options):
            if "candidates" in context:
                phases.append("review")
                assert set(context) == {"candidates", "identity_evidence"}
                assert "体检报告" not in str(context) and "银行卡余额" not in str(context)
                return {"identity_proposals":[proposal([{**first,"quote":"我们约在周五看蓝色草图。"}, second],"correspondence")]}
            result = await super().generate(instructions, context, schema, images, **options)
            event = context.get("event")
            if event:
                phases.append(event["platform"])
                if event["platform"] == "qq":
                    result["identity_proposals"]=[proposal([first])]
                else:
                    assert first_event.text not in str(context)
                    assert first_event.id not in str(context)
            return result
    a,b=QQAdapter(),WXAdapter()
    from companion_core.merged_inbox import MergedInbox
    inbox = MergedInbox(service.store)
    if merged:
        for endpoint in (a,b):
            endpoint.merged=inbox
            endpoint.decision_batch=inbox.for_event
            endpoint.validate_context_event=lambda source: None
    def delivery(event):
        if not merged:
            return event
        last = replace(event, native_id="extra", text="今天聊到这里。",
                       occurred_at=datetime.now(timezone.utc).isoformat())
        events.ingest(service.store,last)
        batch = inbox.freeze([event,last],since=event.occurred_at,generation=core.guard()["generation"])
        assert batch["anchor"] == last.id
        return last
    core=Companion(service.store,TwoPlatformModel(),Host(),[a,b])
    core.start()
    try:
        await core.handle(delivery(first_event))
        graph=IdentityService(service.store)
        assert graph.links()[0]["effective_status"]=="suspected"
        scope = core.governed_context(second_event)
        assert not scope.access.can_read(scope.request,"event/"+first_event.id)
        await core.handle(delivery(second_event))
        assert phases==["qq","review","wechat"]
        assert graph.links()[0]["effective_status"]=="hard"
        assert len(a.sent)==len(b.sent)==1
    finally:
        core.stop()

@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["restricted","unrelated","reclassified_during_review","unseen_quote","wrong_pair"])
async def test_private_candidate_evidence_is_scoped_and_rechecked(service, change):
    from dataclasses import replace
    (a,first),(b,second)=proof(service.store, QQ, WX)
    secret="这是没有提供的身份证资料。"
    a=replace(a,text=a.text+secret)
    first={**first,"quote":"我的微信是wx-test","version":events.ingest(service.store,a)["version"]}
    if change=="unrelated":
        b=replace(b,text="我们约在另一家咖啡店见面。")
        events.ingest(service.store,b)
    graph=IdentityService(service.store)
    graph.apply(proposal([first]))
    resource="event/"+a.id
    def restrict():
        policy=service.repository.policy(resource)
        service.admin.classify(service.request(),resource=resource,expected=policy.version,
                               classification="secret",owners=[QQ.key])
    if change=="restricted":
        restrict()

    class Review(Model):
        async def generate(self,instructions,context,schema,images=(),**options):
            self.contexts.append(context)
            assert secret not in str(context)
            answer=proposal([{**first,"quote":secret if change=="unseen_quote" else "我们约在周五看蓝色草图。"},second],"correspondence")
            if change=="wrong_pair":
                answer["right"]=BILI.as_dict()
            if change=="reclassified_during_review":
                restrict()
            return {"identity_proposals":[answer]}
    model=Review()
    core=Companion(service.store,model,Host(),[Adapter()])
    core.start()
    try:
        if change=="reclassified_during_review":
            with pytest.raises(AssistantError,match="依据已变化"):
                await core._identity_review(b)
        else:
            await core._identity_review(b)
        assert len(model.contexts)==(0 if change in ("unrelated","restricted") else 1)
        assert [link["effective_status"] for link in graph.links()]==["suspected"]
        assert not service.repository.grants(WX.key)
    finally:
        core.stop()

@pytest.mark.asyncio
async def test_shared_clue_matching_two_claimants_stays_pending(service):
    (a, first),(b, _) = proof(service.store, QQ, WX)
    other = replace(a, platform=BILI.platform, actor=BILI, account="bili", native_id="second-claimant")
    source = {**first, "event_id":other.id, "version":events.ingest(service.store,other)["version"]}
    graph=IdentityService(service.store)
    graph.apply(proposal([first]))
    graph.apply({**proposal([source]), "left": BILI.as_dict()})
    model=Model()
    core=Companion(service.store,model,Host(),[Adapter()])
    core.start()
    try:
        await core._identity_review(b)
        assert model.contexts==[]
        assert all(item["effective_status"]=="suspected" for item in graph.links())
    finally:
        core.stop()
