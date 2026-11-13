"""Use fresh contact verification for a claim made while WeChat was offline."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion_core import events
from companion_core.contracts import ActorRef
from companion_core.identity import IdentityService
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.runtime import Companion
from companion_wechat.alias_reviews import AliasReviews
from tests_postgres.test_desktop_management import desktop_store, service
from tests_postgres.test_desktop_manual_identity import QQ, WX, proof, proposal
from tests_postgres.test_runtime import Adapter, Host, Model

__all__=["desktop_store","service"]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed",["never","while_offline","during_review","disconnect"])
async def test_resume_checks_current_alias_then_rechecks_before_commit(service,changed):
    (a,first),(b,second)=proof(service.store,QQ,WX)
    canonical=ActorRef("wechat","wxid_synthetic",WX.scope)
    b=replace(b,actor=canonical,native_id="internal")
    second={**second,"event_id":b.id,"version":events.ingest(service.store,b)["version"]}
    graph=IdentityService(service.store)
    graph.apply(proposal([first]))
    class Receiver:
        account=WX.scope
        bindings=SimpleNamespace(identity="test-login")
        online=False
        current=canonical.user_id
        calls=0
        def status(self):
            return {"state":"connected" if self.online else "retrying"}
        async def resolve_alias(self,hint):
            assert hint==WX.user_id and self.online
            self.calls+=1
            return self.current
    receiver=Receiver()
    scope=ReplyScope("wechat","test-login","private",canonical.user_id)
    ReplyAuthorizations(service.store).update(scope,enabled=True,expected_version=None,actor="fixture")
    class Reviewer(Model):
        async def generate(self,instructions,context,schema,images=(),**options):
            self.contexts.append(context)
            assert context["candidates"][0]["right"]==canonical.as_dict()
            if changed=="during_review":
                receiver.current=None
            if changed=="disconnect":
                receiver.online=False
            return {"identity_proposals":[{**proposal([{**first,"quote":"我们约在周五看蓝色草图。"},second],"correspondence"),
                                            "right":canonical.as_dict()}]}
    model=Reviewer()
    core=Companion(service.store,model,Host(),[Adapter()])
    core.start()
    try:
        await AliasReviews().poll(core,receiver)
        assert receiver.calls==0 and not model.contexts
        receiver.online=True
        if changed=="while_offline":
            receiver.current=None
        await AliasReviews().poll(core,receiver)
        assert receiver.calls==(1 if changed in ("while_offline","disconnect") else 2)
        assert len(graph.person(QQ)["members"])==(2 if changed=="never" else 1)
        assert len(graph.person(WX)["members"])==1
        count=len(model.contexts)
        await AliasReviews().poll(core,receiver)
        assert len(model.contexts)==count
    finally:
        core.stop()
