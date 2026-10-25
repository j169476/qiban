"""Bilibili policy/ledger controller with the unified core as its only owner."""
import json
from hashlib import sha256

from companion_core.errors import AssistantError

from .engine import Engine
from .scheduler import Scheduler


class CoreBoundEngine(Engine):
    """Reuse platform rules without starting or changing the legacy run."""

    def __init__(self, original, core, account):
        # Identical paths may have distinct FileLock instances. Use the owner's Store so
        # native execution can re-enter core guards under the same process lock safely.
        super().__init__(core.store, core.host, original.adapter, core.clock)
        self.core, self.account = core, account

    def validate_start(self):
        self._valid_state()
        if self.store.read("manifest")["data"]["account_uid"] != self.account:
            raise AssistantError("account_changed", "B站绑定账号已改变，请重新建立连接器")
        old = self.store.state("run") or {}
        if old.get("status") == "active":
            raise AssistantError("legacy_runtime_active", "请先暂停 B站旧调度，再显式启动统一核心")
        with self.store.db() as db:
            session = db.execute("SELECT mode FROM sessions WHERE thread=?", (self.identity(),)).fetchone()
        if session and session["mode"] != "role":
            raise AssistantError("mode_denied", "管理聊天不能接管角色运行")

    def validate_run(self, run):
        self.validate_start()
        if ["bilibili", self.account] not in run["accounts"]:
            raise AssistantError("connector_scope", "B站账号不属于本轮核心运行")

    def start(self, *args, **kwargs):
        raise AssistantError("core_owned_lifecycle", "B站连接器由统一核心显式启动")

    def mode(self, required=None):
        self.core.guard()
        if required and required != "role":
            raise AssistantError("mode_denied", "统一核心的 B站连接器仅用于角色运行")
        return "role"

    def run(self, generation=None, check_host=True):
        # Core guards include host activity, generation, platform policy and account binding.
        run = self.core.guard(generation)
        return {**run, "account": self.account}

    def role_run(self):
        return self.run()

    def record_execution_policy(self, run, policy_version, db):
        current = self.store.state(self.core.run_key, db=db)
        if not current or current["generation"] != run["generation"] or current["status"] != "active":
            raise AssistantError("stale_generation", "统一核心运行已经变化")
        current["policy_version"] = policy_version
        self.store.set_state(self.core.run_key, current, db)

    def execution_stopped(self, run):
        return (self.store.runtime / f"core-stop-{run['generation']}").exists()

    def runtime_stopped(self, generation, *, db=None):
        if db is not None:
            self._cancel_pending(db, generation)
            return
        with self.store.db() as db:
            self._cancel_pending(db, generation)

    def core_event_eligible(self, event):
        from companion_core import delivery, events

        from .connector import normalize

        normalized = normalize(self.account, event)
        version = events.get(self.store, normalized.id)["version"]
        if self.store.state("core-processed:" + normalized.id) == version:
            return False
        return delivery.eligible(self.core, normalized, version, self.core.guard())

    def record_core_delivery(self, event, reason, *, deferred):
        from companion_core import delivery

        from .connector import normalize

        normalized = normalize(self.account, event)
        version = sha256(json.dumps(normalized.as_dict(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        record = delivery.defer if deferred else delivery.finish
        record(self.core, normalized, version, self.core.guard(), reason)


class CoreScheduler(Scheduler):
    """Existing content services with model and lifecycle authority supplied by core."""

    def __init__(self, engine, core):
        super().__init__(engine, core.guard()["generation"])
        self.core = core

    async def model(self, instructions, context, output_schema, images=(), *, native=None, guard=None):
        self.alive()
        from companion_core.public_workspace import PublicWorkspace

        if isinstance(self.engine.store, PublicWorkspace):
            from .connector import normalize

            for event in context.get("material", {}).get("events", []):
                if event["kind"] == "dm_reply":
                    raise AssistantError("access_denied", "私信不能进入公开探索模型上下文")
                self.engine.store.direct.add("event/" + normalize(self.engine.account, event).id)
            access_guard = self.engine.store.before_model(context)
            previous_guard = guard

            def guard():
                access_guard()
                if previous_guard:
                    previous_guard()

        return await self.core.model(instructions, context, output_schema, images,
                                     native=native, guard=guard, phase=self.phase)


def companion(engine, model=None):
    """Build a paused/active core view; construction never starts a role run."""
    from companion_codex.model import CodexModel
    from companion_core.runtime import Companion

    from .connector import BilibiliConnector

    return Companion(engine.store, model if model is not None else CodexModel(), engine.host,
                     [BilibiliConnector(engine)], clock=engine.clock)
