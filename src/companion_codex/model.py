"""Codex execution implementation; no platform imports or model policy decisions."""
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

from jsonschema import ValidationError
from jsonschema import validate as validate_json

from companion_core.errors import AssistantError
from companion_core.observability import current_stage, measure
from companion_core.storage import atomic_write


def worker_command(project, output, schema, images=()):
    executable = shutil.which("codex")
    if not executable:
        raise AssistantError("model_unavailable", "未找到 Codex CLI")
    command = [
        executable,
        "exec",
        "--ephemeral",
        "--json",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--cd",
        str(project),
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(output),
        "--color",
        "never",
        "-c",
        'web_search="disabled"',
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "tools.view_image=false",
    ]
    for feature in (
        "shell_tool",
        "plugins",
        "apps",
        "computer_use",
        "browser_use",
        "in_app_browser",
        "multi_agent",
        "image_generation",
        # Images are supplied by the broker through --image. The general image
        # viewer accepts arbitrary local paths even with shell_tool disabled.
        "view_image",
        "hooks",
        "memories",
        "chronicle",
        "skill_search",
        "multi_agent_v2",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "remote_plugin",
    ):
        command += ["--disable", feature]
    # Disable user-configured external MCP tools for the decision-only worker.
    config = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    try:
        settings = tomllib.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        settings = {}
    except (OSError, ValueError):
        raise AssistantError("model_config", "无法读取模型配置，不能确认外部工具已关闭") from None
    for name in settings.get("mcp_servers", {}):
        # Codex splits -c paths on literal dots; TOML-quoting the segment would
        # create a second server named '"name"' without a transport.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise AssistantError("model_config", "MCP 服务名称无法安全用于 CLI 配置覆盖")
        command += ["-c", f"mcp_servers.{name}.enabled=false"]
    if images:
        # A following option terminates --image before the stdin prompt marker.
        offset = command.index("--color")
        command[offset:offset] = [part for path in images for part in ("--image", str(path))]
    # This closes known tool/context paths, not an OS isolation boundary for the
    # whole CLI. P0 must verify the selected Windows backend separately.
    return command + ["-"]


def terminate_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Only this freshly created worker and its descendants; no process-name matching.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def unlink_worker_file(path):
    # Windows may report an exited process before its inherited file handles have
    # finished closing. Retry only this exact worker file, with a fixed deadline.
    deadline = time.monotonic() + 2
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.02)


class CodexModel:
    def __init__(self, command_factory=None, terminator=None, *, reasoning_effort=None, model=None, selection=None):
        if (reasoning_effort not in (None, "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
                or reasoning_effort is not None and command_factory is not None):
            raise AssistantError("model_config", "推理档位仅支持隔离模型入口")
        self.reasoning_effort = reasoning_effort
        if model is not None and (not isinstance(model, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", model)
                or command_factory is not None):
            raise AssistantError("model_config", "单次模型覆盖无效")
        self.model = model
        if selection is not None and (not callable(selection) or command_factory is not None):
            raise AssistantError("model_config", "动态模型选择仅支持隔离模型入口")
        self.selection = selection
        self.isolated = command_factory is None
        self.command_factory = command_factory or worker_command
        self.terminator = terminator or terminate_tree

    async def generate(self, instructions, context, output_schema, images=(), *, native=None,
                       store, policy, guard, phase=lambda value: None):
        with measure(store, current_stage(), kind="model") as observation:
            # Snapshot before awaiting: a settings edit never changes this request mid-flight.
            selected = self.selection() if self.selection is not None else {
                "model": self.model, "effort": self.reasoning_effort}
            from companion_core.reply_trace import point

            point("model_selection", requested_model=selected["model"], effort=selected["effort"],
                  settings_version=selected.get("version"), source=selected.get("source"))
            if self.isolated:
                from .app_server import generate

                return await generate(instructions, context, output_schema, images, native=native,
                                      store=store, policy=policy, guard=guard, phase=phase,
                                      observation=observation, reasoning_effort=selected["effort"], model=selected["model"])
            return await self._generate(instructions, context, output_schema, images, native=native,
                                        store=store, policy=policy, guard=guard, phase=phase,
                                        observation=observation)

    async def _generate(self, instructions, context, output_schema, images=(), *, native=None,
                        store, policy, guard, phase, observation):
        from companion_core.structured_output import restore_optional, strict_schema

        work = store.runtime / "worker"
        work.mkdir(exist_ok=True)
        output = work / f"decision-{uuid.uuid4().hex}.json"
        schema = work / f"schema-{output.stem}.json"
        atomic_write(schema, json.dumps(strict_schema(output_schema)).encode())
        prompt = instructions + "\n" + json.dumps(context, ensure_ascii=False)
        trace = None
        started = time.time()
        deadline = time.monotonic() + policy["worker_timeout_seconds"]
        process = None
        input_file = None
        scope = None
        try:
            guard()
            command = (self.command_factory(work, output, schema, images) if images
                       else self.command_factory(work, output, schema))
            if native:
                from .native import command_profile

                command = command_profile(command, native)
            trace = output.with_suffix(".jsonl").open("wb")
            # A regular, delete-on-close file cannot block writing on an unread
            # anonymous pipe. It also remains bounded when a launcher exits while
            # a descendant keeps an inherited stdin handle open.
            input_file = tempfile.TemporaryFile(mode="w+b", prefix="prompt-", dir=work)
            input_file.write(prompt.encode("utf-8"))
            input_file.seek(0)
            from .process_scope import ProcessScope

            scope = ProcessScope()
            process = subprocess.Popen(
                command,
                stdin=input_file,
                stdout=trace,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                text=True,
                # No launcher code may run before its Job Object owns it.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | (0x4 if os.name == "nt" else 0),
                start_new_session=os.name != "nt",
            )
            scope.attach(process)
            guard()
            if time.monotonic() >= deadline:
                raise AssistantError("model_timeout", "本轮模型处理超时")
            scope.resume()
            while process.poll() is None:
                guard()
                if trace and Path(trace.name).stat().st_size > 8_000_000:
                    raise AssistantError("model_output_size", "模型事件超出本轮上限")
                if native == "web":
                    from .native import evidence

                    if evidence(Path(trace.name), started)["web_calls"] > 6:
                        raise AssistantError("native_tool_limit", "本轮网页工具调用次数超限")
                if time.monotonic() >= deadline:
                    raise AssistantError("model_timeout", "本轮模型处理超时")
                await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))
            guard()
            if Path(trace.name).stat().st_size > 8_000_000:
                raise AssistantError("model_output_size", "模型事件超出本轮上限")
            if process.returncode or not output.exists():
                raise AssistantError("model_failed", "Codex 后台模型调用未成功")
            if output.stat().st_size > 160000:
                raise AssistantError("model_output_size", "模型输出超出本轮上限")
            try:
                decision = restore_optional(json.loads(output.read_text(encoding="utf-8")), output_schema)
                validate_json(decision, output_schema)
            except (ValueError, ValidationError):
                raise AssistantError("model_output_invalid", "模型结果未通过结构化协议校验") from None
            if native:
                from .native import evidence

                trace.flush()
                return {"result": decision, "evidence": evidence(Path(trace.name), started)}
            return decision
        finally:
            # Every cleanup runs even when termination, storage or the heartbeat
            # callback fails. Capture usage before removing the raw event file.
            async with AsyncExitStack() as cleanup:
                cleanup.callback(phase, "cycle")
                cleanup.push_async_callback(asyncio.to_thread, unlink_worker_file, schema)
                cleanup.push_async_callback(asyncio.to_thread, unlink_worker_file, output)
                if trace:
                    from .usage import from_trace

                    cleanup.push_async_callback(asyncio.to_thread, unlink_worker_file, Path(trace.name))
                    cleanup.callback(lambda: observation.tokens(from_trace(Path(trace.name))))
                    cleanup.callback(trace.close)
                if input_file is not None:
                    cleanup.callback(input_file.close)
                try:
                    if scope is not None:
                        await asyncio.to_thread(scope.close)
                finally:
                    if process is not None:
                        await asyncio.to_thread(self.terminator, process)
