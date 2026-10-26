"""Generation-scoped liveness and at-most-once management fault notifications."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

from filelock import FileLock, Timeout

from .errors import AssistantError
from .storage import utcnow

HEARTBEAT_SECONDS = 5
STALE_SECONDS = 90
BENIGN_STOPS = {
    "user_stop", "chat_archived", "run_suspended", "not_running", "stale_generation",
    "host_session_end", "duration_complete",
}
FAULT_TEXT = {
    "login_required": "B站登录已失效或尚未完成，需要重新登录。",
    "credential_invalid": "本机登录凭证无效，需要重新登录。",
    "credential_refresh_required": "平台要求更新登录，但缺少续期条件，需要重新登录。",
    "credential_refresh_uncertain": "凭证续期未能确认完成，请重新登录后再启动。",
    "keyring_unavailable": "系统凭证库暂不可用，请检查本机凭证存储。",
    "account_mismatch": "当前登录账号与项目绑定账号不一致。",
    "account_changed": "项目绑定账号已改变，需要核对后显式启动。",
    "repeated_failure": "后台连续三轮失败，已暂停，请查看管理状态中的最近错误。",
    "inbox_repeated_failure": "收件连续三次失败，已保留待办并暂停，请查看管理状态中的收件错误。",
    "private_compression_failed": "本机对话整理连续三次失败，已保留原文并暂停，请在管理网页查看整理状态。",
    "backup_failed": "自动备份连续三次失败，已有备份已保留，请在管理网页查看备份状态。",
    "storage_unavailable": "数据库连接曾中断，连接恢复后角色仍保持暂停，请检查本机数据库。",
    "daemon_exited": "后台进程异常退出，已保留进度并暂停。",
    "heartbeat_lost": "后台心跳中断，已保留进度并暂停。",
    "supervisor_lost": "后台监测进程中断，已保留进度并暂停。",
    "critical_state_invalid": "本地关键配置未通过校验，请修复结构化文件。",
    "host_unavailable": "暂时无法核实 Codex 聊天状态，已暂停后台。",
    "host_timeout": "核实 Codex 聊天状态超时，已暂停后台。",
    "host_protocol_error": "Codex 宿主接口检查失败，已暂停后台。",
    "setup_incomplete": "后台运行条件不完整，请查看管理状态。",
    "unexpected_daemon_error": "后台遇到未处理错误，已保留进度并暂停。",
}


def age(record, now=None):
    try:
        stamp = datetime.fromisoformat(record["at"])
        return max(0, ((now or datetime.now(timezone.utc)) - stamp).total_seconds())
    except (KeyError, TypeError, ValueError):
        return float("inf")


class Heartbeat:
    """A separate thread also ticks while a synchronous host/file check is busy."""

    def __init__(self, store, generation, kind="health"):
        self.store, self.generation, self.kind = store, generation, kind
        self.key = f"{kind}:{generation}"
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.values = {
            "generation": generation, "pid": os.getpid(), "phase": "starting",
            "started_at": utcnow(), "last_success_at": None, "failures": 0,
            "last_error": None, "wait_reason": None, "next_cycle_at": None,
        }
        self.thread = None

    def pulse(self, **values):
        with self.lock:
            self.values.update(values)
            self.store.set_state(self.key, {**self.values, "at": utcnow()})

    def __enter__(self):
        self.pulse()

        def tick():
            while not self.stopped.wait(HEARTBEAT_SECONDS):
                try:
                    self.pulse()
                except Exception:
                    # No raw errors (including OS keyring paths) in daemon output.
                    # The independent monitor detects a missing heartbeat.
                    pass

        self.thread = threading.Thread(target=tick, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join(timeout=2)
        self.pulse(phase="stopped", next_cycle_at=None)


def faults(store, limit=10):
    records = list(store.states("fault:%").values())
    return sorted(records, key=lambda r: r["created_at"], reverse=True)[:limit]


def record_fault(engine, code, generation):
    if code in BENIGN_STOPS:
        return None
    store = engine.store
    with store.lock, store.db() as db:
        key = f"fault:{generation}"
        old = store.state(key, db=db)
        if old:
            return old
        run = store.state("run", {}, db)
        if run.get("generation") != generation:
            return None
        fault = {
            "id": generation, "generation": generation, "account": run.get("account", ""),
            "code": code if code in FAULT_TEXT else "unexpected_daemon_error",
            "created_at": utcnow(), "owner": run["owner"], "notification": "queued",
        }
        store.set_state(key, fault, db)
    store.audit("runtime", {"event": "fault", **fault})
    return fault


def fail(engine, code, generation):
    run = engine.store.state("run", {})
    if run.get("generation") != generation:
        return None
    if run.get("status") != "active":
        return record_fault(engine, run.get("reason", code), generation)
    engine.stop_run(code, generation, paused=True)
    return record_fault(engine, code, generation)


def reconcile(engine):
    """Read/open/status may suspend a dead run, never launch or deliver a message."""
    store = engine.store
    run = store.state("run")
    if not run or run["status"] != "active":
        return run
    generation = run["generation"]
    if engine.expire_run(run):
        return store.state("run")
    if (store.runtime / f"stop-{generation}").exists():
        engine.stop_run("run_suspended", generation, paused=True)
        return store.state("run")
    if not run.get("background_requested"):
        return run
    launched = {"at": run.get("launch_requested_at")}
    supervisor = store.state(f"supervisor:{generation}")
    health = store.state(f"health:{generation}")
    # Allow a new child to acquire the OS lock and publish its first pulse.
    if age(launched) <= STALE_SECONDS and (not supervisor or not health):
        return run
    code = None
    if not supervisor or age(supervisor) > STALE_SECONDS or supervisor["phase"] == "stopped":
        code = "supervisor_lost"
    elif not health or age(health) > STALE_SECONDS or health["phase"] == "stopped":
        code = "heartbeat_lost"
    if code:
        fail(engine, code, generation)
    return store.state("run")


def check_supervisor(store, run):
    if not run.get("background_requested"):
        return
    supervisor = store.state(f"supervisor:{run['generation']}")
    if not supervisor and age({"at": run.get("launch_requested_at")}) <= STALE_SECONDS:
        return
    if age(supervisor) > STALE_SECONDS or supervisor.get("phase") == "stopped":
        raise AssistantError("supervisor_lost", "监测进程心跳失效，保持暂停直到显式启动")


def status(engine):
    run = reconcile(engine)
    generation = (run or {}).get("generation", "")
    heartbeat = engine.store.state(f"health:{generation}")
    supervisor = engine.store.state(f"supervisor:{generation}")
    return {
        "heartbeat": heartbeat, "supervisor": supervisor,
        "inbox": engine.store.state(f"inbox:{generation}"),
        "heartbeat_age_seconds": round(age(heartbeat), 1) if heartbeat else None,
        "recovery": "explicit_start_only", "faults": faults(engine.store),
        "notifications": notification_status(engine.store),
        "credential": engine.store.state(
            f"credential-health:{engine.store.read('manifest')['data']['account_uid']}"
        ),
    }


def notification_destination(store, *, db=None):
    """One current management binding; legacy config overrides never route alerts."""
    if db is None:
        with store.db() as connection:
            return notification_destination(store, db=connection)
    missing = object()
    destination = store.state("service-admin-thread", missing, db=db)
    if destination is missing:
        if store.state("web-owner", db=db):
            return None  # An initialized service must bind its own manager.
        destination = store.state("management_thread", db=db)  # Standalone legacy engine.
    if not isinstance(destination, str) or not destination:
        return None
    bound = db.execute("SELECT mode FROM sessions WHERE thread=?", (destination,)).fetchone()
    return destination if bound and bound["mode"] == "admin" else None


def notification_status(store):
    return {"mode": store.read("companion")["data"].get("failure_notifications", "off"),
            "routing": "current_management", "destination": notification_destination(store)}


def deliver_faults(engine):
    """Only an opted-in, previously bound admin destination can receive an alert.

    Reserve before the external call. A lost response is uncertain and is never
    retried automatically: avoiding duplicate interruption takes priority.
    """
    store = engine.store
    delivery_lock = FileLock(str(store.runtime / "notification.lock"), timeout=0)
    try:
        delivery_lock.acquire()
    except Timeout:
        return []
    delivered = []
    try:
        for fault in reversed(faults(store, limit=100)):
            if fault["notification"] != "queued":
                continue
            setting_record = store.read("companion")
            if setting_record["data"].get("failure_notifications", "off") != "admin_once":
                break
            destination = notification_destination(store)
            if destination is None:
                break
            try:
                # This does not resume, unarchive, start a turn or change configuration.
                engine.host.notification_ready(destination)
            except AssistantError:
                continue
            with store.lock, store.db() as db:
                if store.read("companion")["version"] != setting_record["version"]:
                    continue
                if notification_destination(store, db=db) != destination:
                    continue
                current = store.state(f"fault:{fault['id']}", db=db)
                if not current or current["notification"] != "queued":
                    continue
                # This reservation fixes the destination for this one attempt.
                # Rebinding affects queued faults, never replays an in-flight one.
                current.update(notification="dispatching", destination=destination, attempted_at=utcnow())
                store.set_state(f"fault:{fault['id']}", current, db)
            state_note = ("备份会按退避计划重试，角色运行状态没有因此改变。\n" if fault["code"] == "backup_failed" else
                          "角色已暂停，记忆和学习进度保存在本地。修复后仍需用户在角色聊天显式启动。\n")
            text = (
                f"栖伴故障通知 [{fault['id']}]\n{FAULT_TEXT[fault['code']]}\n"
                + state_note +
                "这是用户授权的单次管理提醒。请简短向用户呈现故障；不执行修复、不启动角色、"
                "不向任何其他聊天发送消息。普通重试无需提醒。"
            )
            try:
                receipt = engine.host.notify_admin(destination, text, fault["id"])
                current.update(notification="delivered", receipt=receipt)
                delivered.append(fault["id"])
            except Exception:
                current["notification"] = "uncertain"
            current["completed_at"] = utcnow()
            store.set_state(f"fault:{fault['id']}", current)
        return delivered
    finally:
        delivery_lock.release()


def start_notifier(engine):
    """Launch only a management notifier after a fault; never launches a role."""
    import subprocess
    import sys

    store = engine.store
    try:
        if store.read("companion")["data"].get("failure_notifications", "off") != "admin_once":
            return False
        if not any(f["notification"] == "queued" for f in faults(store, 100)):
            return False
        with FileLock(str(store.runtime / "notification-launch.lock"), timeout=0):
            beat = store.state("notifier-health:notifications") or {}
            if (
                age(store.state("notifier")) < STALE_SECONDS
                or beat.get("phase") != "stopped" and age(beat) < STALE_SECONDS
            ):
                return False
            store.set_state("notifier", {"at": utcnow(), "status": "starting"})
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "bili_assistant", "--project", str(engine.host.project),
                     "--data", str(store.root), "notifications"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                store.set_state("notifier", {"status": "launch_failed"})
                return False
        return True
    except (AssistantError, Timeout):
        return False


async def notification_loop(engine):
    import asyncio
    import time

    store = engine.store
    lock = FileLock(str(store.runtime / "notification-loop.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        return
    try:
        # A closed/archived/busy management chat does not justify waking the role.
        # Keep the fault durable after this bounded delivery window.
        deadline = time.monotonic() + 24 * 3600
        with Heartbeat(store, "notifications", "notifier-health"):
            while time.monotonic() < deadline:
                store.set_state("notifier", {"at": utcnow(), "status": "waiting"})
                if store.read("companion")["data"].get("failure_notifications", "off") != "admin_once":
                    return
                if not any(f["notification"] == "queued" for f in faults(store, 100)):
                    return
                await asyncio.to_thread(deliver_faults, engine)
                if not any(f["notification"] == "queued" for f in faults(store, 100)):
                    return
                await asyncio.sleep(60)
    finally:
        store.set_state("notifier", {"status": "stopped"})
        lock.release()
