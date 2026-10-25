"""Session-scoped, restartable private summarization with transactional provenance.

The transport supplies a freshly authenticated request and a reauthentication
callback. This module never discovers users, accepts model-selected scopes, or
compares private conversations. Original turns remain authoritative.
"""
from __future__ import annotations

import secrets
from types import SimpleNamespace

from . import conversations
from . import memory_maintenance as algorithm
from .access import AccessService
from .errors import AssistantError
from .governance import GovernanceRepository, ReadView, record_provenance, verify_record_versions
from .schema import obj


class ConversationMaintenance:
    interval = 30
    lease_seconds = 600

    def __init__(self, core, authority):
        self.core, self.store, self.authority = core, core.store, authority
        self.repository = GovernanceRepository(self.store)
        self.access = AccessService(self.repository, authority)
        self.busy = False

    @staticmethod
    def key(account, channel, conversation):
        return "private-compression:" + conversations.thread_id(account, channel, conversation)

    def status(self, account, channel, conversation):
        state = self.store.state(self.key(account, channel, conversation), {})
        # No actor/session IDs, message IDs, summary text or model exceptions.
        return {key: state.get(key) for key in
                ("status", "next_at", "failures", "error", "updated_at", "last_saved_at")}

    def _view(self, request):
        with self.store.db() as db:
            scoped = {row[0] for row in db.execute(
                "SELECT resource FROM governance_policies WHERE conversation=? AND audience='private' "
                "AND (resource LIKE 'conversation_turns/%' OR resource LIKE 'conversation_summaries/%')",
                (request.conversation,))}
        readable = self.repository.readable_resources(self.access, request, candidates=scoped)
        # Filter metadata before decrypting; even separately authorized memories
        # and other conversations cannot become summarizer input.
        return ReadView(self.store, readable & scoped)

    async def step(self, request, *, account, channel, authenticate):
        if self.busy:
            return None
        self.authority.require(request)
        if (getattr(self.store, "backend", None) != "postgres" or request.administrator
                or not request.actor or not request.session_id or request.audience != "private"
                or request.purpose != "reply" or request.recipients != {request.actor}):
            raise AssistantError("compression_scope", "私人整理需要当前已认证的角色会话")
        self.busy = True
        try:
            return await self._step(request, account, channel, authenticate)
        finally:
            self.busy = False

    async def _step(self, request, account, channel, authenticate):
        run = self.core.guard()
        settings = self.store.read("companion")
        if (run["read_only"] or [channel, account] not in run["accounts"]
                or not settings["data"].get("memory_maintenance", True)):
            return None
        key = self.key(account, channel, request.conversation)
        state = self.store.state(key, {})
        now = self.core.clock().timestamp()
        if state.get("next_at", 0) > now and state.get("generation") == run["generation"]:
            return None
        if not self.core.remaining_model_calls():
            return None
        configs = {("companion", None): settings["version"],
                   **{(kind, None): self.store.version(kind) for kind in ("policy", "manifest")}}
        versions, lease, attempt = {}, None, None

        def guard():
            current_run = self.core.guard(run["generation"])
            self.authority.require(request)
            current = authenticate()
            self.authority.require(current)
            if (current_run["read_only"] or current_run["owner"] != run["owner"]
                    or any(getattr(current, name) != getattr(request, name) for name in
                           ("actor", "conversation", "audience", "recipients", "purpose", "session_id", "epoch"))):
                raise AssistantError("session_expired", "私人整理的会话或运行已经改变")
            if attempt and self.store.state(key, {}).get("attempt") != attempt:
                raise AssistantError("context_changed", "整理任务已被新的运行接管")
            if lease:
                self.access.revalidate(lease)
            verify_record_versions(self.store, {**configs, **versions})

        try:
            guard()
            tid = conversations.thread_id(account, channel, request.conversation)
            engine = SimpleNamespace(store=self._view(request), clock=self.core.clock)
            job = algorithm.compression_job(engine, account=account, thread=tid)
            if not job:
                with self.store.lock, self.store.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    guard()
                    if self.store.state(key, {}) == state:
                        self.store.set_state(key, {**state, "generation": run["generation"], "status": "idle",
                            "next_at": now + self.interval, "failures": 0, "error": "", "updated_at": now})
                return None
            previous = ({"id": job["previous"]["data"]["id"], "version": job["previous"]["version"]}
                        if job["previous"] else None)
            target = conversations.identifier("summary", tid, previous, job["spans"])
            # An existing but revoked/edited summary must never be resurrected
            # by the algorithm's idempotent save path or get a new permission.
            if self.store.exists("conversation_summaries", target):
                return self._failure(key, state, attempt, run, "context_changed", True)
            roots = ["conversation_turns/" + turn["data"]["id"] for turn in job["turns"]]
            if job["previous"]:
                roots.append("conversation_summaries/" + job["previous"]["data"]["id"])
            lease = self.access.lease(request, roots)
            versions.update({("conversation_turns", turn["data"]["id"]): turn["version"] for turn in job["turns"]})
            for summary in job["chain"]:
                versions["conversation_summaries", summary["data"]["id"]] = summary["version"]
                versions.update({("conversation_turns", ref["id"]): ref["version"] for ref in summary["data"]["turn_refs"]})
            pins = [(resource, dict(lease.versions)[resource]) for resource in dict.fromkeys(roots)]
            with self.store.lock, self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                guard()
                if self.store.state(key, {}) != state:
                    return None
                attempt = secrets.token_hex(16)
                self.store.set_state(key, {**state, "attempt": attempt, "generation": run["generation"],
                    "status": "running", "next_at": now + self.lease_seconds, "updated_at": now})
            result = await self.core.model(algorithm.SUMMARY_PROMPT, algorithm.compression_payload(job),
                                          obj({"summary": {**algorithm.text(job["summary_limit"]), "minLength": 1}}), guard=guard)
            if not result["summary"].strip():
                raise AssistantError("invalid_summary", "空白摘要不能表示已经覆盖原文")
            with self.store.lock, self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                guard()
                # Refresh membership as well as pins: new earlier turns or source
                # revocations must not be hidden by the pre-call read view.
                engine.store = self._view(request)
                if self.store.exists("conversation_summaries", target):
                    raise AssistantError("context_changed", "摘要已由另一入口保存或修改")
                saved = algorithm.save_compression(engine, job, result)
                record_provenance(self.store, "conversation_summaries", saved,
                    {"owners": [request.actor], "conversation": request.conversation,
                     "audience": "private", "pins": pins})
                self.access.lease(request, ["conversation_summaries/" + saved])
                finished = self.core.clock().timestamp()
                self.store.set_state(key, {"attempt": attempt, "generation": run["generation"], "status": "saved",
                    "next_at": finished + self.interval, "failures": 0, "error": "", "updated_at": finished,
                    "last_saved_at": finished})
            return {"status": "saved", "saved": saved}
        except AssistantError as exc:
            benign = exc.code in {"access_denied", "context_changed", "edit_conflict", "session_expired",
                "login_required", "not_running", "run_suspended", "stale_generation", "chat_archived",
                "duration_complete", "model_quota"}
            return self._failure(key, state, attempt, run, "context_changed" if benign else "compression_failed", benign)
        except Exception:
            # Do not persist provider error messages, which may contain prompts.
            return self._failure(key, state, attempt, run, "compression_failed", False)

    def _failure(self, key, state, attempt, run, code, benign):
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.store.state(key, {})
            if (attempt and current.get("attempt") != attempt) or (not attempt and current != state):
                return None
            failures = 0 if benign else state.get("failures", 0) + 1
            now = self.core.clock().timestamp()
            status = "deferred" if benign else "failed"
            self.store.set_state(key, {**state, "attempt": attempt, "generation": run["generation"],
                "status": status, "next_at": now + (self.interval if benign else min(1800, 60 * 2 ** min(failures - 1, 5))),
                "failures": failures, "error": code, "updated_at": now})
            return {"status": status, "failures": failures, "error": code}
