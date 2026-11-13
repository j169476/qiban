"""Risk preview and single-use, account-bound confirmation for desktop reply scopes."""

import secrets
import time
from dataclasses import asdict

from companion_core.errors import AssistantError
from companion_core.reply_audience import Audience, change, preview, read


class DesktopAudiences:
    def __init__(self, manager):
        self.manager = manager
        self.pending = None

    def category(self, platform, kind):
        if platform not in ("qq", "wechat") or kind not in ("private", "group"):
            raise AssistantError("management_request", "未知回复范围")
        if platform == "wechat":
            from .desktop_wechat import _profile, selection
            store, install = self.manager.store, self.manager.installation
            login = selection(store,install)["selected"]
            if login:
                _, key = _profile(store,install,login)
                source = store.state(key)
                if source:
                    return Audience("wechat",source["identity"],kind)
            raise AssistantError("reply_setup_required", "请先选择微信账号，读取并保存一次回复名单。名单可以为空。")
        store = self.manager.store
        saved = store.state("desktop-qq-account", {})
        selected = saved.get("selected")
        if not selected or selected["uin"] not in saved.get("configurations", {}):
            raise AssistantError("reply_setup_required", "请先核验并保存 QQ 的回复会话，可保存空名单。")
        account = selected["uin"]
        return Audience(platform, account, kind)

    def status(self, platform):
        try:
            first = self.category(platform, "private")
        except AssistantError as exc:
            if exc.code != "reply_setup_required":
                raise
            return {"ready": False, "message": str(exc)}
        result = {
            "ready": True,
            "account": first.account_identity,
            "categories": {
                kind: read(self.manager.store, Audience(platform, first.account_identity, kind))
                for kind in ("private", "group")
            },
        }
        if platform == "wechat":
            from .desktop_wechat import selection
            result["display_account"] = selection(self.manager.store,self.manager.installation)["selected"]
        return result

    def prepare(self, request, platform, kind, enabled, account):
        if type(enabled) is not bool:
            raise AssistantError("management_request", "请选择开启或关闭")
        category = self.category(platform, kind)
        if category.account_identity != account:
            raise AssistantError("edit_conflict", "账号已变化，请重新查看当前范围")
        value = preview(self.manager.store, category, enabled)
        selection_revision = None
        if platform == "wechat":
            from .desktop_wechat import selection
            selected = selection(self.manager.store,self.manager.installation)
            selection_revision = selected["revision"]
            value["message"] = value["message"].replace(account,selected["selected"])
            if enabled:
                value["message"] += "已移除的对象重新成为好友或重新入群后，也会补处理本次授权范围内仍可读取的积压消息。"
        token = secrets.token_hex(16)
        self.pending = (request.session_id, time.monotonic() + 120, token, value, selection_revision)
        return {**value, "token": token}

    def save(self, request, token):
        pending = self.pending
        if (
            not pending
            or pending[0] != request.session_id
            or time.monotonic() > pending[1]
            or not isinstance(token, str)
            or not secrets.compare_digest(pending[2], token)
        ):
            raise AssistantError("edit_conflict", "请重新核对回复范围")
        value = pending[3]
        category = self.category(value["scope"]["platform"], value["scope"]["kind"])
        if category.platform == "wechat":
            from .desktop_wechat import selection
            if selection(self.manager.store,self.manager.installation)["revision"] != pending[4]:
                raise AssistantError("edit_conflict","微信账号选择已改变，请重新核对")
        if asdict(category) != value["scope"]:
            raise AssistantError("edit_conflict", "登录账号已变化，请重新核对")
        with self.manager.store.db() as db:
            if self.manager.store.state("core-run:" + category.platform, {}, db).get("status") == "active":
                raise AssistantError("platform_active", "请先停止该平台，再修改持续回复范围")
            result = change(
                self.manager.store,
                category,
                value["enabled"],
                value["expected"],
                actor=request.actor,
                confirmed=value["scope"],
                db=db,
            )
        self.pending = None
        return result
