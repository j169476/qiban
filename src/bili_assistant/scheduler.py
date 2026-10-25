"""Plugin-owned scheduler: no detached native heartbeat to clean up on archive."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

from filelock import FileLock, Timeout

from companion_codex.model import terminate_tree, worker_command

from .cognition import (
    DECISION_PROMPT,
    DECISION_SCHEMA,
    REFLECTION_PROMPT,
    REFLECTION_SCHEMA,
    packet,
    reflection_input,
    save_reflection,
    verify_versions,
)
from .errors import AssistantError
from .media import validated_image_paths


def readiness(engine):
    # run() verifies the owner's current state through the official App Server.
    # Hooks are optional acceleration; missing hook support must not disable polling.
    try:
        run = engine.run()
    except AssistantError as exc:
        if exc.code != "account_changed":
            raise
        return {"ready": False, "issues": [str(exc)], "lifecycle": "host_polling", "poll_seconds": 5}
    hook = engine.store.runtime / "hooks" / f"{run['owner']}.json"
    issues = []
    if not run["account"]:
        issues.append("尚未登录和绑定 B站账号")
    elif run["account"] != engine.store.read("manifest")["data"]["account_uid"]:
        issues.append("账号绑定已改变，请显式重新启动角色")
    if not shutil.which("codex"):
        issues.append("找不到 Codex CLI")
    return {
        "ready": not issues,
        "issues": issues,
        "lifecycle": "hooks_and_polling" if hook.exists() else "host_polling",
        "poll_seconds": 5,
        "hook_observed": hook.exists(),
    }


def launch(engine):
    with FileLock(str(engine.store.runtime / "launch.lock"), timeout=20):
        return _launch(engine)


def _launch(engine):
    checks = readiness(engine)
    if not checks["ready"]:
        return checks
    run = engine.run()
    lock = FileLock(str(engine.store.runtime / "supervisor.lock"), timeout=0)
    try:
        with lock:
            pass
    except Timeout:
        if engine.store.state(f"supervisor:{run['generation']}"):
            return {**checks, "reused": True, "generation": run["generation"]}
    from .supervisor import command

    # Reserve startup before spawning, so simultaneous explicit calls cannot spawn twice.
    if run.get("background_requested"):
        return {**checks, "reused": True, "generation": run["generation"]}
    with engine.store.lock:
        run.update(background_requested=True, launch_requested_at=datetime.now(timezone.utc).isoformat())
        engine.store.set_state("run", run)
    try:
        with (engine.store.runtime / "daemon.log").open("ab") as log:
            process = subprocess.Popen(
                command(engine, "supervise", run["generation"]),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except OSError:
        from .health import fail

        fail(engine, "daemon_exited", run["generation"])
        raise AssistantError("daemon_exited", "后台监测进程未能启动") from None
    return {
        **checks,
        "pid": process.pid,
        "generation": run["generation"],
        "model_route": "codex-exec-ephemeral",
        "usage": "使用当前 Codex 配置的模型和计费",
    }






class Scheduler:
    def __init__(self, engine, generation):
        self.engine, self.generation = engine, generation
        self.heartbeat = None
        self.collector = None

    def phase(self, name, **values):
        if self.heartbeat:
            self.heartbeat.pulse(phase=name, **values)

    def alive(self):
        return self.engine.run(self.generation)

    def remaining_model_calls(self):
        from companion_core.budget import remaining

        store = self.engine.store
        policy = store.read("policy")["data"]
        self.engine.run(check_host=False)
        return remaining(store, policy, self.engine.clock())

    async def model(self, instructions, context, output_schema, images=(), *, native=None, guard=None):
        self.alive()
        self.phase("model")
        store = self.engine.store
        policy = store.read("policy")["data"]
        from companion_core.budget import reserve

        reserve(store, policy, self.engine.clock(), account=self.engine.run(check_host=False)["account"])
        from companion_codex.model import CodexModel
        from companion_core.decision import DecisionService

        def alive():
            self.alive()
            if guard:
                guard()

        service = DecisionService(CodexModel(worker_command, terminate_tree))
        return await service.generate(instructions, context, output_schema, images, native=native,
                                      store=store, policy=policy, guard=alive, phase=self.phase)

    async def decide(self, material, references=()):
        from .connector import review_identities
        await review_identities(self, material.get("events", []))
        from companion_core.recall_routing import resolve

        from .memory_maintenance import valid_groups
        from .rich_content import event_context

        # Fetch rich context only for selected inbox events, not every notification.
        enriched = []
        for event in material.get("events", []):
            if event.get("attachments") or hasattr(self.engine.adapter, "comment_page"):
                current = await event_context(self.engine, event["id"])
            else:
                current = event
            if event.get("video_reference"):
                # Rich context reloads the immutable raw inbox row, whose short
                # link has no BV/page yet. Reuse the source-validated derivation
                # instead of overwriting it with the raw empty/default fields.
                current = await self.engine.resolve_invitation(current)
                if current.get("video_reference") != event["video_reference"]:
                    raise AssistantError("context_changed", "读取视频期间邀请来源已变化，请重新选择视频")
            enriched.append({**event, **current})
        material["events"] = enriched
        image_order = []
        video = material.get("video")
        reserved = len(video.get("images", [])) if video and video["coverage"]["kind"] == "frames" else 0
        for event in material["events"]:
            supplied = event.get("images", [])
            event["images"] = supplied[: max(0, 4 - reserved - len(image_order))]
            event["images_omitted"] = event.get("images_omitted", 0) + len(supplied) - len(event["images"])
            image_order.extend(event["images"])
        if video:
            # Preserve all preview sheets when their time range is the video's sole evidence.
            supplied = video.get("images", [])
            video["images"] = supplied[: max(0, 4 - len(image_order))]
            video["images_omitted"] = len(supplied) - len(video["images"])
            image_order.extend(video["images"])
        material["image_order"] = image_order

        references = list(references)
        settings = self.engine.store.read("companion")["data"]
        memory_count = sum(len(self.engine.store.records(k)[0])
                           for k in ("memories", "knowledge", "skills", "relationships", "diaries", "interest_signals"))
        query = " ".join(
            [(material.get("video") or {}).get("title", "")]
            + [e.get("text", "") for e in material.get("events", [])]
        )
        uids = {event["uid"] for event in material.get("events", [])}
        if material.get("video"):
            uids.add(material["video"]["owner_uid"])
        from .conversations import event_thread, thread_id

        account = self.engine.run(check_host=False)["account"]
        threads = sorted({thread_id(account, *event_thread(e)) for e in material.get("events", [])})
        expanded = await resolve(
            self.engine.store, query, scope={"platform": "bilibili", "account": account, "threads": threads},
            uids=uids, eligible=bool(memory_count > settings["catalog_limit"] or valid_groups(self.engine.store)[0]),
            remaining=self.remaining_model_calls(), generate=self.model, guard=self.alive, clock=self.engine.clock)
        for reference in expanded["references"]:
            if reference not in references:
                references.append(reference)
        if expanded["queries"]:
            for record in self.engine.store.search(query, limit=8, queries=expanded["queries"], uids=uids):
                reference = {"kind": record["kind"], "id": record["data"]["id"]}
                if reference not in references:
                    references.append(reference)
        context, versions = packet(self.engine, material, references, queries=expanded["queries"])
        images = validated_image_paths(self.engine.store, image_order)
        decision = await self.model(DECISION_PROMPT, context, DECISION_SCHEMA, images)
        self.alive()
        verify_versions(self.engine.store, versions)
        # Internal provenance is attached only after validating the model output.
        # The persistence and dispatch stages must not lose this snapshot.
        decision["_context_versions"] = versions
        return decision

    async def maybe_reflect(self):
        self.alive()
        store = self.engine.store
        account = self.engine.run(check_host=False)["account"]
        pending = store.state(f"reflection-pending:{account}")
        if pending:
            try:
                save_reflection(self.engine, **pending)
            except AssistantError as exc:
                if exc.code != "context_changed":
                    raise
                store.set_state(f"reflection-pending:{account}", None)
                store.audit("learning", {"event": "reflection_conflict", **exc.as_dict()})
            return
        if not self.remaining_model_calls():
            return
        job = reflection_input(self.engine)
        if not job:
            return
        context, versions = packet(self.engine, {})
        context["reflection_memories"] = job["memories"]
        versions += [("memories", item["data"]["id"], item["version"]) for item in job["memories"]]
        result = await self.model(REFLECTION_PROMPT, context, REFLECTION_SCHEMA)
        self.alive()
        verify_versions(self.engine.store, versions)
        growth_version = next(version for kind, _, version in versions if kind == "growth")
        save_reflection(self.engine, job, result, growth_version)

    async def cycle(self):
        from companion_core.orchestration import run_cycle

        from .workflow import BilibiliWorkflow

        return await run_cycle(self.engine.store, [BilibiliWorkflow(self).definition()],
                               guard=self.alive, remaining_model_calls=self.remaining_model_calls,
                               phase=self.phase)

    async def maintenance(self):
        """One bounded job, alternating memory organization and source review."""
        from .knowledge import maintain as review
        from .memory_maintenance import maintain as memory

        account = self.engine.run(check_host=False)["account"]
        key = f"maintenance-lane:{account}"
        next_lane = self.engine.store.state(key, 0)
        lanes = (memory, review) if next_lane == 0 else (review, memory)
        for lane in lanes:
            result = await lane(self)
            if result:
                self.engine.store.set_state(key, 1 if lane is memory else 0)
                return result
        return None

    async def run(self):
        # A file lock is held by the process; OS releases it after crashes.
        lock = FileLock(str(self.engine.store.runtime / "daemon.lock"), timeout=45)
        try:
            lock.acquire()
        except Timeout:
            return
        collector_task = None
        try:
            from .health import Heartbeat, fail
            from .inbox_worker import InboxWorker

            with Heartbeat(self.engine.store, self.generation) as heartbeat:
                self.heartbeat = heartbeat
                self.alive()
                self.engine.store.set_state("daemon_generation", self.generation)
                ready = readiness(self.engine)
                if not ready["ready"]:
                    raise AssistantError("setup_incomplete", "; ".join(ready["issues"]))
                self.collector = InboxWorker(self.engine, self.generation)
                collector_task = asyncio.create_task(self.collector.run())
                next_cycle = 0.0
                failures = 0
                while True:
                    self.alive()
                    if time.monotonic() >= next_cycle or self.collector.wake.is_set():
                        self.collector.wake.clear()
                        try:
                            self.phase("credentials", wait_reason=None)
                            if hasattr(self.engine.adapter, "maintain_credentials"):
                                await self.engine.adapter.maintain_credentials(self.alive()["account"])
                            self.phase("cycle")
                            await self.cycle()
                            failures = 0
                            heartbeat.pulse(last_success_at=datetime.now(timezone.utc).isoformat(),
                                            failures=0, last_error=None)
                        except AssistantError as exc:
                            self.engine.store.audit("runtime", {"event": "cycle_error", **exc.as_dict()})
                            heartbeat.pulse(last_error=exc.code)
                            if exc.code in (
                                "critical_state_invalid", "login_required", "credential_invalid",
                                "credential_refresh_required", "credential_refresh_uncertain",
                                "keyring_unavailable", "account_mismatch", "account_changed",
                                "chat_archived", "run_suspended", "stale_generation", "not_running",
                                "supervisor_lost", "duration_complete",
                            ):
                                raise
                            if exc.code not in (
                                "interval", "outside_hours", "quota_exhausted", "model_quota",
                                 "video_quota", "action_disabled", "reply_loop_limit", "credential_busy", "cycle_busy",
                            ):
                                failures += 1
                            heartbeat.pulse(failures=failures, wait_reason=exc.code)
                            if failures >= 3:
                                raise AssistantError("repeated_failure", "连续三轮失败，暂停后台活动")
                        seconds = self.engine.store.read("policy")["data"]["cycle_minutes"] * 60
                        from .delivery import next_delay

                        seconds = min(seconds, next_delay(self.engine, seconds))
                        next_cycle = time.monotonic() + seconds
                        self.phase("waiting", next_cycle_at=(
                            datetime.now(timezone.utc) + timedelta(seconds=seconds)
                        ).isoformat())
                    await asyncio.sleep(5)
        except AssistantError as exc:
            fail(self.engine, exc.code, self.generation)
        except Exception:
            fail(self.engine, "unexpected_daemon_error", self.generation)
        finally:
            if collector_task:
                collector_task.cancel()
                await asyncio.gather(collector_task, return_exceptions=True)
            self.collector = None
            self.heartbeat = None
            lock.release()
        if self.engine.store.state(f"fault:{self.generation}"):
            from .health import start_notifier

            start_notifier(self.engine)
