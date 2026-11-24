"""Wire the pinned legacy transport to the desktop installation's verified account."""
import json

from filelock import FileLock

from bili_assistant.bilibili import Bilibili
from bili_assistant.browser_login_transport import crypt
from bili_assistant.connector import BilibiliConnector
from bili_assistant.credentials import FIELDS, validate_credential
from bili_assistant.engine import Engine
from companion_core.errors import AssistantError
from companion_core.storage import atomic_write

from .desktop_bili_policy import BiliRuntimeStore


class BoundCredentials:
    def __init__(self, broker, uid):
        self.broker, self.uid = broker, uid

    def _current(self, expected=None):
        value = self.broker._account()
        if not value or value["uid"] != self.uid or expected is not None and value["revision"] != expected:
            raise AssistantError("account_changed","B 站绑定账号已改变，请重新核对。")
        return value

    def revision(self):
        return self._current()["revision"]

    def load(self, expected=None):
        from bilibili_api import Credential

        value = self._current(expected)
        credential = Credential(**value["cookies"])
        validate_credential(credential)
        if str(credential.dedeuserid) != self.uid:
            raise AssistantError("account_mismatch","B 站凭据与绑定账号不符。")
        return credential

    def save_refresh(self, credential, expected):
        validate_credential(credential)
        if str(credential.dedeuserid) != self.uid:
            raise AssistantError("account_mismatch","续期返回了不同的 B 站账号。")
        with self.broker.lock, FileLock(str(self.broker.path)+".lock",timeout=3):
            current = self._current(expected)
            current["cookies"] = {key:getattr(credential,key,None) for key in FIELDS}
            atomic_write(self.broker.path,crypt(json.dumps(current,ensure_ascii=False).encode(),self.broker.binding))


class DesktopBiliConnector(BilibiliConnector):
    async def runtime_poll(self, *, receive=True):
        from companion_core import delivery, events
        core=self.engine.core
        run=core.guard()
        batch=await core.poll(receive=receive)
        permitted=[]
        blocked=core.store.read("preferences")["data"]
        for event in batch:
            kind=event.metadata["kind"]
            with core.store.db() as db:
                terminal=db.execute("SELECT 1 FROM actions WHERE account=? AND dedupe=? AND status IN "
                                    "('succeeded','uncertain','dispatching')",
                                    (self.account,f"{self.account}:{kind}:{event.native_id}")).fetchone()
            consumed=terminal or core.store.state(f"processed:{self.account}:{event.native_id}")
            if (consumed or kind not in {"reply","dm_reply"} or event.actor.user_id in [self.account,*blocked["blocked_uids"]]
                or any(t.casefold() in event.text.casefold() for t in blocked["blocked_topics"] if t)):
                delivery.finish(core,event,events.get(core.store,event.id)["version"],run,"prior_receipt_or_ineligible")
            else:
                permitted.append(event)
        return permitted

    async def public_wake(self,core):
        from .desktop_public_tools import wake
        await wake(core,self)

    def workflow(self,core):
        from companion_core.orchestration import Flow, Stage, Workflow
        async def receive(state):
            incoming=await self.runtime_poll()
            if incoming:
                from companion_core.reply_batch import handle_batch
                await handle_batch(core,incoming[:core.store.read("policy")["data"]["items_per_cycle"]])
                return Flow.STOP
        async def public_timer(state):
            await core.role.react("public_timer",adapter=self)
        return Workflow(self.platform,self.account,(Stage("role_inbox",receive,0,"prepare"),
                                                   Stage("public_timer",public_timer,1,requires_model=True)))

    def backlog_workflow(self, core):
        from companion_core.orchestration import Stage, Workflow
        from companion_core.reply_batch import handle_batch
        async def drain(state):
            await handle_batch(core,await self.runtime_poll(receive=False))
        return Workflow(self.platform,self.account,(Stage("role_backlog",drain,0,"prepare"),))

    async def close(self):
        from .desktop_bili import close_sdk_session
        await close_sdk_session()


def adapter(broker):
    current = broker._account()
    if not current:
        return None
    uid = current["uid"]
    store = BiliRuntimeStore(broker.manager.store,uid)
    transport = Bilibili(store,BoundCredentials(broker,uid))
    # bind_runtime installs the application-owned host before any work is admitted.
    return DesktopBiliConnector(Engine(store,None,transport),account=uid)
