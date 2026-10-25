"""Shared explicit-start/stop and generation ownership for compatibility controllers."""
import json
import uuid
from datetime import datetime, timedelta

from .errors import AssistantError
from .storage import atomic_write, utcnow


class RunLifecycle:
    def start(self, duration_minutes=None, read_only=None):
        if duration_minutes is not None and (
            type(duration_minutes) is not int or not 1 <= duration_minutes <= 1440
        ):
            raise AssistantError("invalid_duration", "运行时长须为 1 至 1440 分钟")
        if read_only is not None and type(read_only) is not bool:
            raise AssistantError("invalid_read_only", "read_only 须为布尔值")
        self.open("role")
        self._valid_state()
        tid = self.identity()
        account = self.store.read("manifest")["data"]["account_uid"]
        with self.store.lock, self.store.db() as db:
            old = self.store.state("run", db=db)
            if (
                old
                and old["owner"] == tid
                and old["status"] == "active"
                and old["account"] == account
                and not (self.store.runtime / f"stop-{old['generation']}").exists()
            ):
                if (
                    read_only is not None and read_only != old.get("read_only", False)
                    or duration_minutes is not None and duration_minutes != old.get("duration_minutes")
                ):
                    raise AssistantError("run_options_changed", "先暂停当前运行，再显式启动不同模式或时长")
                return {"run": old, "reused": True}
            if old:
                self._cancel_pending(db, old["generation"])
            run = {
                "owner": tid,
                "generation": uuid.uuid4().hex,
                "status": "active",
                "account": account,
                "started_at": self.clock().isoformat(),
                "pending_since": (
                    old.get("pending_since", old["started_at"])
                    if old and old["account"] == account and old["owner"] == tid
                    else self.clock().isoformat()
                ),
                "policy_version": self.store.read("policy")["version"],
                "read_only": bool(read_only),
            }
            if duration_minutes is not None:
                run.update(
                    duration_minutes=duration_minutes,
                    deadline_at=(self.clock() + timedelta(minutes=duration_minutes)).isoformat(),
                )
            self.store.set_state("run", run, db)
        self.store.audit("runtime", {"event": "start", **run})
        return {"run": run, "reused": False}


    def _cancel_pending(self, db, generation):
        db.execute(
            "UPDATE actions SET status='cancelled' WHERE generation=? AND status='prepared'", (generation,)
        )
        db.execute(
            """UPDATE actions SET status='uncertain',result=? WHERE generation=? AND status='dispatching'""",
            (self.store.ledger_encode("actions.result", {"error": "interrupted", "message": "中断时未确认平台结果，不能自动重发"}),
             generation),
        )


    def stop(self, reason="user_stop", paused=False):
        self.mode()
        return self.stop_run(reason, paused=paused)


    def stop_run(self, reason, generation=None, paused=False):
        run = self.store.state("run")
        if not run or (generation and run["generation"] != generation):
            return {"stopped": False}
        # Write first, without the DB/action lock, so in-flight model work can stop promptly.
        atomic_write(self.store.runtime / f"stop-{run['generation']}", reason.encode("utf-8"))
        with self.store.lock, self.store.db() as db:
            current = self.store.state("run", db=db)
            if current and current["generation"] == run["generation"]:
                current.update(status="paused" if paused else "stopped", reason=reason, ended_at=utcnow())
                self._cancel_pending(db, run["generation"])
                self.store.set_state("run", current, db)
        return {"stopped": True, "run": current}


    def _valid_state(self):
        issues = self.store.validate_critical()["issues"]
        if any(i.get("critical") for i in issues):
            raise AssistantError("critical_state_invalid", "关键配置无效，先修复本地文件")


    def expire_run(self, run):
        if run.get("deadline_at") and self.clock() >= datetime.fromisoformat(run["deadline_at"]):
            self.stop_run("duration_complete", run["generation"], paused=True)
            return True
        return False


    def run(self, generation=None, check_host=True):
        run = self.store.state("run")
        if not run or run["status"] != "active":
            raise AssistantError("not_running", "角色运行尚未开始或已经停止")
        if generation and run["generation"] != generation:
            raise AssistantError("stale_generation", "旧运行已被新角色聊天接管")
        if self.expire_run(run):
            raise AssistantError("duration_complete", "本次运行已到指定时限，保持暂停")
        if (self.store.runtime / f"stop-{run['generation']}").exists():
            raise AssistantError("run_suspended", "收到宿主结束或停止标记，请显式重新启动")
        fallback = self.store.runtime / "hooks" / f"end-{run['owner']}.json"
        if fallback.exists():
            try:
                event = json.loads(fallback.read_text(encoding="utf-8"))
                if datetime.fromisoformat(event["at"]) >= datetime.fromisoformat(run["started_at"]):
                    raise AssistantError("run_suspended", "宿主会话已结束")
            except (ValueError, KeyError):
                raise AssistantError("run_suspended", "宿主结束标记无效") from None
        if check_host:
            try:
                self.host.require_active(run["owner"])
            except AssistantError as exc:
                self.stop_run(exc.code, run["generation"], paused=True)
                raise
        self._valid_state()
        if self.store.read("manifest")["data"]["account_uid"] != run["account"]:
            raise AssistantError("account_changed", "账号绑定已改变，请核对后显式重新启动")
        self.check_supervisor(run)
        return run


    def role_run(self):
        self.mode("role")
        run = self.run()
        if run["owner"] != self.identity():
            raise AssistantError("not_owner", "此聊天已失去本轮运行归属")
        return run
