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
    def backlog_workflow(self, core):
        from bili_assistant.governed_workflow import definition
        return definition(self.engine, core, receive=False)

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
