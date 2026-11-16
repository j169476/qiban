"""Local QQ login management. No role, message session, or model is started here."""
from copy import deepcopy
from pathlib import Path
from threading import Event, RLock, Thread

from companion_core.errors import AssistantError
from companion_qq.config import qq_id
from companion_qq.native_login import NativeLogin
from companion_qq.native_process import NativeProcess

from . import desktop_qq_targets as targets

KEY = "desktop-qq-account"


class DesktopQQ:
    def __init__(self, store, runtime, data, *, worker_factory=NativeLogin, target_factory=NativeProcess):
        self.store, self.runtime, self.data = store, Path(runtime), Path(data)
        self.worker_factory = worker_factory
        self.target_factory = target_factory
        self.preview = None
        self.directory = None
        self.lock, self.cancelled = RLock(), Event()
        self.thread = self.worker = None
        self.closed = False
        self.value = {"state": "idle", "message": "尚未检查 QQ 登录状态。", "accounts": []}

    def status(self):
        with self.lock:
            saved = self.store.state(KEY, {"revision": 0, "selected": None})
            options = targets.saved_options(self, saved)
            permissions = targets.ReplyAuthorizations(self.store)
            return {**deepcopy(self.value), "running": self.store.state("core-run:qq", {}).get("status") == "active", "busy": bool(self.thread and self.thread.is_alive()),
                    "selected": saved["selected"], "revision": saved["revision"],
                    "directory": deepcopy(self.directory),
                    "targets": {"private_targets": options["private_targets"], "groups": options["groups"],
                                "authorized": all(permissions.version(scope) for scope in targets.scopes(options))}
                               if options else None,
                    "preview": {"token": self.preview["token"],
                                "private_targets": self.preview["options"]["private_targets"],
                                "groups": self.preview["options"]["groups"]} if self.preview else None}

    def _set(self, **value):
        with self.lock:
            self.value = value

    def begin(self, operation, account=None, password=None):
        if operation not in {"refresh", "login", "password"}:
            raise AssistantError("qq_login_state", "未知 QQ 登录操作。")
        credentials = None
        if operation == "password":
            if (not isinstance(password, str) or not 1 <= len(password) <= 128 or "\0" in password):
                raise AssistantError("qq_login_input", "请输入有效的 QQ 号和密码。")
            credentials = {"account": qq_id(account), "password": password}
        with self.lock, self.store.lock:
            self._editable()
            self.cancelled.clear()
            self.preview = None
            self.directory = None
            self.value = {"state": "connecting", "message": "正在连接 QQ 登录服务……", "accounts": []}
            self.thread = Thread(target=self._run, args=(operation,), kwargs={"credentials": credentials}, daemon=True)
            self.thread.start()
        return {"started": True}

    def verify_targets(self, private_targets, groups, expected):
        with self.lock, self.store.lock:
            self._editable()
            saved = self.store.state(KEY, {"revision": 0, "selected": None})
            if type(expected) is not int or saved["revision"] != expected:
                raise AssistantError("edit_conflict", "QQ 账号或会话设置已改变，请重新读取。")
            options = targets.configuration(self, saved, private_targets, groups)
            self.preview = None
            self.cancelled.clear()
            self.value = {"state": "checking_targets", "message": "正在核验好友与群，请稍候……", "accounts": []}
            self.thread = Thread(target=self._run, args=("targets", options, expected), daemon=True)
            self.thread.start()
        return {"started": True}

    def _editable(self):
        if self.closed or self.thread and self.thread.is_alive():
            raise AssistantError("qq_login_busy", "请等待 QQ 账号操作完成。")
        if self.worker is not None:
            raise AssistantError("qq_cleanup_failed", "QQ 后台未能正常退出，请重启应用后台后再试。")
        if self.store.state("core-run:qq", {}).get("status") == "active":
            raise AssistantError("qq_stop_required", "请先停止 QQ 运行，再修改账号或回复会话。")

    def load_directory(self, expected):
        with self.lock, self.store.lock:
            self._editable()
            saved = self.store.state(KEY, {"revision": 0, "selected": None})
            if type(expected) is not int or saved["revision"] != expected:
                raise AssistantError("edit_conflict", "QQ 账号选择已改变，请重新读取。")
            options = targets.configuration(self, saved, [], [])
            self.directory = None
            self.cancelled.clear()
            self.value = {"state": "loading_directory", "message": "正在读取好友和群名称……", "accounts": []}
            self.thread = Thread(target=self._run, args=("directory", options, expected), daemon=True)
            self.thread.start()
        return {"started": True}

    def save_targets(self, token, expected, actor):
        with self.lock:
            with self.store.lock, self.store.db() as db:
                self._editable()
                db.execute("BEGIN IMMEDIATE")
                saved = self.store.state(KEY, {"revision": 0, "selected": None}, db)
                if type(expected) is not int or saved["revision"] != expected:
                    raise AssistantError("edit_conflict", "QQ 账号或会话设置已改变，请重新核验。")
                if self.preview is None or not isinstance(token, str) or token != self.preview["token"]:
                    raise AssistantError("qq_target_unverified", "请先核验要保存的回复会话。")
                self.store.set_state(KEY, targets.save(self, self.preview, saved, actor, db), db)
            self.preview = None
            self.value.update(state="targets_saved", message="回复会话已保存。")
            return self.status()

    def _run(self, operation, options=None, revision=None, credentials=None):
        worker = None
        try:
            worker = (self.target_factory(targets.native_parameters(options)) if options is not None
                      else self.worker_factory({"runtime": str(self.runtime), "data": str(self.data)}))
            with self.lock:
                self.worker = worker
            if self.cancelled.is_set():
                return
            if operation == "directory":
                directory = targets.directory(worker, options)
                with self.lock:
                    if not self.cancelled.is_set():
                        self.directory = directory
                        self.value = {"state": "directory_ready", "message": "好友和群名称已读取。请选择要添加的会话。", "accounts": []}
                worker.request("close")
                if worker.process is not None:
                    worker.process.wait(5)
                return
            if operation == "targets":
                preview = targets.verify(self, worker, options, revision)
                with self.lock:
                    if not self.cancelled.is_set():
                        self.preview = preview
                        self.value = {"state": "targets_verified", "message": "会话核验通过，请确认后保存回复范围。", "accounts": []}
                worker.request("close")
                if worker.process is not None:
                    worker.process.wait(5)
                return
            authenticated_uin = None
            worker.open()
            if self.cancelled.is_set():
                return
            if operation in {"login", "password"}:
                if operation == "password":
                    try:
                        worker.request("login_password", **credentials)
                    finally:
                        credentials.clear()
                else:
                    worker.request("login_qr")
                while not self.cancelled.is_set():
                    result = worker.request("login_status")
                    state = result["status"]
                    if state == "authenticated":
                        authenticated_uin = result["uin"]
                        self._set(state="syncing_account", message="登录已确认，正在同步账号……", accounts=[])
                        break
                    if state in {"failed", "expired", "cancelled", "verification_required"}:
                        message = ("QQ 要求额外的图形或手机设备验证，请切换扫码登录完成验证。" if state == "verification_required"
                                   else "密码登录未完成，请核对账号密码或切换扫码。" if operation == "password"
                                   else "二维码已失效，请重新点击扫码登录。")
                        self._set(state=state, message=message, accounts=[])
                        return
                    self._set(state=state, message="请用手机 QQ 扫码并确认登录。" if state == "qr_ready"
                                else "已扫码，请在手机上确认登录。" if state == "qr_scanned"
                                else "正在验证账号密码……" if operation == "password"
                                else "正在获取二维码……", accounts=[], **({"png_base64": result["png_base64"]}
                              if state in {"qr_ready", "qr_scanned"} else {}))
                    self.cancelled.wait(.3)
            if self.cancelled.is_set():
                return
            accounts = worker.request("login_accounts")["accounts"]
            # The QR success callback can precede QQ's persisted login-list update.
            # Never present an unrelated cached account as the newly scanned account.
            if authenticated_uin:
                for _ in range(40):
                    if any(a["uin"] == authenticated_uin and a["can_quick_login"] for a in accounts):
                        break
                    if self.cancelled.wait(.25):
                        return
                    accounts = worker.request("login_accounts")["accounts"]
                else:
                    self._set(state="error", error="qq_account_sync_pending", accounts=[],
                              message="登录已确认，账号尚未同步完成，请点击检查已登录账号。")
                    return
            if self.cancelled.is_set():
                return
            self._set(state="available", message="请选择要用于栖伴的 QQ 账号。" if accounts
                      else "尚无可用 QQ 账号，请扫码登录。", accounts=accounts, authenticated_uin=authenticated_uin)
            worker.request("close")
            if worker.process is not None:
                worker.process.wait(5)
        except Exception as exc:
            if not self.cancelled.is_set():
                code = getattr(exc, "code", "qq_login_failed")
                self.preview = None
                if operation == "directory":
                    self.directory = None
                self._set(state="error", error=code, accounts=[], message=("QQ 组件尚未准备好。" if
                    code == "qq_native_resources_invalid" or isinstance(exc, FileNotFoundError)
                    else "好友和群列表读取失败，请检查账号与网络后重试。" if operation == "directory"
                    else "QQ 会话核验未完成，请核对号码、账号与网络后重试。" if operation == "targets"
                    else "QQ 登录操作未完成，请检查网络后重试。"))
        finally:
            if credentials is not None:
                credentials.clear()
            try:
                if worker is not None:
                    worker.close()
            except Exception:
                with self.lock:
                    self.preview = self.directory = None
                    self.value = {"state": "error", "error": "qq_cleanup_failed", "accounts": [],
                                  "message": "QQ 后台未能正常退出，请重启应用后台后再试。"}
            else:
                with self.lock:
                    self.worker = None
                    if self.cancelled.is_set():
                        self.preview = self.directory = None
                        self.value = {"state": "cancelled", "message": "QQ 账号操作已取消。", "accounts": []}

    def select(self, account, expected):
        account = qq_id(account)
        with self.lock, self.store.lock:
            self._editable()
            saved = self.store.state(KEY, {"revision": 0, "selected": None})
            if type(expected) is not int or saved["revision"] != expected:
                raise AssistantError("edit_conflict", "QQ 账号选择已改变，请重新检查。")
            matches = [row for row in self.value.get("accounts", []) if row["uin"] == account and row["can_quick_login"]]
            if len(matches) != 1:
                raise AssistantError("qq_login_required", "该账号不可恢复登录，请重新扫码。")
            if self.store.state("core-run:qq", {}).get("status") == "active":
                raise AssistantError("qq_stop_required", "请先停止 QQ 运行，再切换 QQ 账号。")
            selected = {key: matches[0][key] for key in ("uin", "uid", "label")}
            self.store.set_state(KEY, {**saved, "revision": expected + 1, "selected": selected})
            self.preview = None
            self.directory = None
            self.value.update(state="selected", message="QQ 账号已保存。")
        return self.status()

    def cancel(self):
        with self.lock:
            busy = bool(self.thread and self.thread.is_alive())
            if not busy:
                return {"cancel_requested": False}
            self.cancelled.set()
            worker = self.worker
            self.value = {"state": "cancelling", "message": "正在取消 QQ 登录……", "accounts": []}
        if worker is not None:
            worker.close()
        return {"cancel_requested": True}

    def close(self):
        self.closed = True
        self.cancel()
        if self.thread:
            self.thread.join(10)
            if self.thread.is_alive():
                raise AssistantError("qq_login_stopping", "QQ 登录后台仍在退出。")
