"""Durable explicit task control with immutable stage records and fresh ACL checks.

Only a trusted transport can submit tasks. A workflow cannot choose owners,
release private data publicly, change permissions, or start a role run.
"""
import json
import re
import secrets
from dataclasses import asdict

from jsonschema import ValidationError, validate

from .access import AccessService, RecordPolicy
from .contracts import ActorRef, stable_key
from .errors import AssistantError
from .governance import GovernanceRepository, ReadView, verify_record_versions
from .task_contracts import REQUESTS


class ExplicitTasks:
    def __init__(self, core, authority, *, authenticate, session_active, handler):
        self.core, self.store, self.authority = core, core.store, authority
        self.authenticate, self.session_active, self.handler = authenticate, session_active, handler
        self.repository = GovernanceRepository(self.store)
        self.access = AccessService(self.repository, authority)
        self.busy = False

    def _state(self, identifier):
        state = self.store.state("explicit-task:" + identifier)
        if not state:
            raise AssistantError("not_found", "任务不存在或不可访问")
        return state

    def _save(self, identifier, state):
        state = {**state, "revision": secrets.token_hex(16)}
        self.store.set_state("explicit-task:" + identifier, state)
        return state

    def _classify(self, resource, request, sources):
        pins = tuple((key, version) for key, version in sources if key != resource)
        current = self.repository.policy(resource)
        self.repository.set_policy(RecordPolicy(resource, secrets.token_hex(16), "conversation",
            frozenset((request.actor,)), request.conversation, "private", tuple(key for key, _ in pins),
            source_versions=pins), current.version if current else None, "explicit-task")

    def submit(self, request, actor, kind, arguments, nonce):
        self.authority.require(request)
        if (request.actor != actor.key or request.audience != "private" or request.recipients != {actor.key}
                or request.administrator or request.purpose != "reply"):
            raise AssistantError("access_denied", "任务必须来自已验证的私密会话")
        if kind not in REQUESTS or not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{32}", nonce):
            raise AssistantError("task_request", "任务类型或提交标识无效")
        try:
            validate(arguments, REQUESTS[kind])
            json.dumps(arguments, allow_nan=False)
        except (ValidationError, ValueError, TypeError):
            raise AssistantError("task_request", "任务参数不符合约束") from None
        if kind == "video" and arguments["end_seconds"] <= arguments["start_seconds"]:
            raise AssistantError("task_request", "视频结束时间必须晚于开始时间")
        run = self.core.guard()
        if run["read_only"]:
            raise AssistantError("read_only", "只读运行不能创建角色任务")
        identifier = stable_key("task", actor.key, nonce)
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.store.exists("task_requests", identifier):
                old = self.store.read("task_requests", identifier)["data"]
                if old["actor"] != asdict(actor) or old["kind"] != kind or old["arguments"] != arguments:
                    raise AssistantError("task_conflict", "提交标识已被另一任务使用")
                self.access.lease(request, ["task_requests/" + identifier])
                return {"id": identifier, "duplicate": True}
            if sum(self._state(row["data"]["id"])["status"] in {"queued", "running"}
                   for row in self.store.records("task_requests")[0]) >= 12:
                raise AssistantError("task_queue_full", "待办任务最多 12 项")
            now = self.core.clock().isoformat()
            record = self.store.write("task_requests", {"schema_version": 1, "id": identifier, "actor": asdict(actor),
                "conversation": request.conversation, "kind": kind, "arguments": arguments,
                "source": {"type": "user_instruction", "recorded_at": now, "reference": nonce}, "locked": False},
                None, "explicit-user", identifier)
            self._classify("task_requests/" + identifier, request, ())
            self._save(identifier, {"status": "queued", "phase": "start", "step": 0, "latest": "",
                "request_version": record["version"],
                "generation": run["generation"], "owner": run["owner"], "session": request.session_id,
                "error": "", "attempts": 0})
        return {"id": identifier, "duplicate": False}

    def detail(self, request, identifier):
        self.authority.require(request)
        roots = ["task_requests/" + identifier]
        self.access.lease(request, roots)
        record = self.store.read("task_requests", identifier)
        versions = {("task_requests", identifier): record["version"]}
        state = self._state(identifier)
        result = None
        if state["latest"]:
            roots.append("task_results/" + state["latest"])
        lease = self.access.lease(request, roots)
        if state["latest"]:
            item = self.store.read("task_results", state["latest"])
            versions["task_results", state["latest"]] = item["version"]
            result = item["data"]
        self.access.revalidate(lease)
        verify_record_versions(self.store, versions)
        return {"id": identifier, "kind": record["data"]["kind"], "arguments": record["data"]["arguments"],
                "status": state["status"], "phase": state["phase"], "error": state["error"],
                "revision": state["revision"], "result": result}

    def listing(self, request):
        readable = self.repository.readable_resources(self.access, request)
        rows = ReadView(self.store, readable).records("task_requests")[0]
        rows.sort(key=lambda row: row["data"]["source"]["recorded_at"], reverse=True)
        result = []
        for row in rows[:50]:
            try:
                result.append(self.detail(request, row["data"]["id"]))
            except AssistantError as exc:
                if exc.code not in {"access_denied", "context_changed"}:
                    raise
        return result

    def control(self, request, identifier, action, expected):
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.access.lease(request, ["task_requests/" + identifier])
            old = self._state(identifier)
            if old["revision"] != expected:
                raise AssistantError("edit_conflict", "任务状态已变化，请刷新后操作")
            if action == "cancel" and old["status"] in {"queued", "running"}:
                return self._save(identifier, {**old, "status": "cancelled", "error": "user_cancelled"})
            if action == "retry" and old["status"] in {"failed", "cancelled"} and old["attempts"] < 3:
                if self.store.version("task_requests", identifier) != old["request_version"]:
                    raise AssistantError("task_request_changed", "原始要求已修改，请明确提交新任务")
                run = self.core.guard()
                if run["read_only"]:
                    raise AssistantError("read_only", "本次运行只读")
                return self._save(identifier, {**old, "status": "queued", "error": "", "session": request.session_id,
                    "generation": run["generation"], "owner": run["owner"]})
            raise AssistantError("task_control", "此任务不能执行该操作")

    def stop(self, reason, *, session=None):
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in self.store.records("task_requests")[0]:
                identifier = row["data"]["id"]
                old = self._state(identifier)
                if old["status"] in {"queued", "running"} and (session is None or old["session"] == session):
                    self._save(identifier, {**old, "status": "cancelled", "error": reason})

    async def step(self):
        if self.busy:
            return False
        self.busy = True
        try:
            for record in self.store.records("task_requests")[0]:
                identifier = record["data"]["id"]
                state = self._state(identifier)
                if state["status"] == "running":
                    self._save(identifier, {**state, "status": "failed", "error": "interrupted_no_automatic_retry"})
                elif state["status"] == "queued":
                    await self._execute(record, state)
                    return True
            return False
        finally:
            self.busy = False

    async def _execute(self, record, state):
        identifier = record["data"]["id"]
        current = state
        try:
            if record["version"] != state["request_version"] or record["data"]["locked"]:
                raise AssistantError("task_request_changed", "原始要求已改变或锁定")
            actor = ActorRef(**record["data"]["actor"])
            request = self.authenticate(actor, record["data"]["conversation"], state["session"])
            roots = ["task_requests/" + identifier]
            versions = {("task_requests", identifier): record["version"]}
            previous = None
            if state["latest"]:
                previous = self.store.read("task_results", state["latest"])
                roots.append("task_results/" + state["latest"])
                versions["task_results", state["latest"]] = previous["version"]
            lease = self.access.lease(request, roots)
            configs = {(kind, None): self.store.version(kind) for kind in self.store.profile.config}

            def guard():
                run = self.core.guard(state["generation"])
                if (run["owner"] != state["owner"] or run["read_only"] or not self.session_active(state["session"])
                        or self._state(identifier)["revision"] != current["revision"]):
                    raise AssistantError("task_cancelled", "任务会话或运行已结束")
                # Recheck the stored session's actor and purpose as well as its
                # revocation bit; the issuer alone is not an identity proof.
                self.authenticate(actor, record["data"]["conversation"], state["session"])
                self.access.revalidate(lease)
                verify_record_versions(self.store, {**versions, **configs})

            with self.store.lock, self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                guard()
                current = self._save(identifier, {**state, "status": "running", "attempts": state["attempts"] + 1})
            value = await self.handler(record["data"], previous["data"] if previous else None,
                                       request=request, guard=guard)
            with self.store.lock, self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                guard()
                step = state["step"] + 1
                result_id = stable_key("task-result", identifier, step)
                data = {"schema_version": 1, "id": result_id, "task_id": identifier, "step": step,
                    "phase": value["phase"], "payload": value["payload"], "summary": value.get("summary", ""),
                    "complete": value["complete"], "locked": False,
                    "source": {"type": "task", "recorded_at": self.core.clock().isoformat(), "reference": identifier}}
                if len(json.dumps(data, ensure_ascii=False)) > 100_000:
                    raise AssistantError("task_result_size", "任务结果超过本轮容量")
                extra = dict(value.get("source_pins", ()))
                extra_lease = self.access.lease(request, extra)
                if any(dict(extra_lease.versions).get(key) != version for key, version in extra.items()):
                    raise AssistantError("context_changed", "任务实际材料已改变")
                self.store.write("task_results", data, None, "explicit-task", result_id)
                # Keep direct roots only; every prior stage remains reachable.
                pins = tuple({**{key: dict(lease.versions)[key] for key in roots}, **extra}.items())
                self._classify("task_results/" + result_id, request, pins)
                if data["complete"] and data["summary"]:
                    memory_id = stable_key("task-memory", identifier)
                    self.store.write("memories", {"schema_version": 1, "id": memory_id, "kind": "experience",
                        "content": ("用户明确安排的任务结果（保留材料与模型自检的局限）：\n" + data["summary"])[:4000],
                        "confidence": "observed", "tags": ["explicit_task"], "locked": False,
                        "source": {"type": "interaction", "recorded_at": self.core.clock().isoformat(),
                                   "reference": identifier}}, None, "explicit-task", memory_id)
                    final_policy = self.repository.policy("task_results/" + result_id)
                    self._classify("memories/" + memory_id, request, [(final_policy.resource, final_policy.version)])
                self._save(identifier, {**current, "status": "completed" if data["complete"] else "queued",
                    "phase": data["phase"], "step": step, "latest": result_id, "attempts": 0, "error": ""})
        except AssistantError as exc:
            with self.store.lock, self.store.db():
                if self._state(identifier)["revision"] == current["revision"]:
                    self._save(identifier, {**current, "status": "failed", "error": exc.code})
        except Exception:
            with self.store.lock, self.store.db():
                if self._state(identifier)["revision"] == current["revision"]:
                    self._save(identifier, {**current, "status": "failed", "error": "task_failed"})
