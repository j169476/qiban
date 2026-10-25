import asyncio
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import FakeHost

from bili_assistant.engine import Engine
from bili_assistant.errors import AssistantError
from bili_assistant.host import CodexHost
from bili_assistant.scheduler import Scheduler, readiness, worker_command


def test_background_can_poll_without_hooks(role, monkeypatch):
    monkeypatch.setattr("bili_assistant.scheduler.shutil.which", lambda _: sys.executable)
    result = readiness(role)
    assert result["ready"] and result["lifecycle"] == "host_polling"
    assert not result["hook_observed"]
    # No synthetic hook marker is needed or written.
    assert not (role.store.runtime / "hooks" / "role.json").exists()


@pytest.mark.parametrize("failure", ["chat_archived", "host_unavailable"])
def test_polling_cannot_start_with_inactive_or_unknown_host(role, monkeypatch, failure):
    def unavailable(*args):
        raise AssistantError(failure, "unavailable")

    monkeypatch.setattr(role.host, "require_active", unavailable)
    with pytest.raises(AssistantError) as exc:
        readiness(role)
    assert exc.value.code == failure
    assert role.store.state("run")["status"] == "paused"


def test_background_rejects_changed_account(role, monkeypatch):
    monkeypatch.setattr("bili_assistant.scheduler.shutil.which", lambda _: sys.executable)
    manifest = role.store.read("manifest")
    manifest["data"]["account_uid"] = "999"
    role.store.write("manifest", manifest["data"], manifest["version"], "test")
    assert not readiness(role)["ready"]


def test_worker_disables_existing_mcp_without_creating_quoted_server(monkeypatch, tmp_path):
    (tmp_path / "config.toml").write_text('[mcp_servers.node_repl]\ncommand="node"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr("bili_assistant.scheduler.shutil.which", lambda _: sys.executable)
    command = worker_command(tmp_path, tmp_path / "out.json", tmp_path / "schema.json")
    assert "mcp_servers.node_repl.enabled=false" in command
    assert not any('"node_repl"' in part for part in command)


def test_worker_rejects_ambiguous_mcp_override_path(monkeypatch, tmp_path):
    (tmp_path / "config.toml").write_text('[mcp_servers."a.b"]\ncommand="node"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr("bili_assistant.scheduler.shutil.which", lambda _: sys.executable)
    with pytest.raises(AssistantError) as exc:
        worker_command(tmp_path, tmp_path / "out.json", tmp_path / "schema.json")
    assert exc.value.code == "model_config"


def test_concurrent_clients_send_once(role):
    a = role.prepare("like", {"bvid": "BV1xx411c7mD"})
    other = Engine(type(role.store)(role.store.root), FakeHost(), role.adapter, role.clock)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda engine: asyncio.run(engine.execute(a["id"])), (role, other)))
    assert sum(bool(r["sent"]) for r in results) == 1
    assert len(role.adapter.sent) == 1


def test_session_end_is_a_hint_and_does_not_stop_role_or_admin(role, tmp_path):
    source = Path(__file__).resolve().parents[1] / "plugins" / "qiban" / "hooks" / "lifecycle.py"
    plugin = tmp_path / "plugin"
    (plugin / "hooks").mkdir(parents=True)
    shutil.copy(source, plugin / "hooks" / "lifecycle.py")
    (plugin / "runtime.json").write_text(json.dumps({"project": str(tmp_path), "data": str(role.store.root)}))
    generation = role.run()["generation"]

    def hook(thread, event):
        begin = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(plugin / "hooks" / "lifecycle.py")],
            input=json.dumps({"session_id": thread, "cwd": str(tmp_path), "hook_event_name": event}),
            text=True,
            capture_output=True,
            timeout=3,
        )
        assert result.returncode == 0
        assert time.monotonic() - begin < 3

    hook("admin", "SessionEnd")
    assert not (role.store.runtime / f"stop-{generation}").exists()
    hook("role", "SessionStart")
    assert (role.store.runtime / "hooks" / "role.json").exists()
    hook("role", "SessionEnd")
    assert not (role.store.runtime / f"stop-{generation}").exists()
    event = json.loads((role.store.runtime / "hooks" / "role.json").read_text(encoding="utf-8"))
    assert event["event"] == "SessionEnd" and event["thread_id"] == "role"
    assert role.run()["generation"] == generation


def test_host_paginates_all_sources(tmp_path):
    class Stub(CodexHost):
        def request(self, method, params):
            if method == "thread/read":
                return {"thread": {"cwd": str(tmp_path), "id": "target"}}
            assert params["sourceKinds"] == []
            if params["archived"]:
                if not params.get("cursor"):
                    return {"data": [], "nextCursor": "page2"}
                return {"data": [{"id": "target"}], "nextCursor": None}
            pytest.fail("should have found archived thread")

    assert Stub(tmp_path).status("target")["archived"]


@pytest.mark.asyncio
async def test_model_cancelled_on_archive(role, monkeypatch, tmp_path):
    import bili_assistant.scheduler as module

    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\nimport sys\nsys.stdin.read()\ntime.sleep(60)\n")
    monkeypatch.setattr(
        module, "worker_command", lambda project, output, schema: [sys.executable, str(sleeper)]
    )
    stopped = []
    original = module.terminate_tree

    def capture(process):
        original(process)
        stopped.append(process.poll() is not None)

    monkeypatch.setattr(module, "terminate_tree", capture)
    original_run = role.run
    worker_started = False
    original_popen = module.subprocess.Popen

    def start_worker(*args, **kwargs):
        nonlocal worker_started
        process = original_popen(*args, **kwargs)
        worker_started = True
        return process

    monkeypatch.setattr(module.subprocess, "Popen", start_worker)

    def archive_after_model_starts(*args, **kwargs):
        if worker_started:
            role.host.states["role"] = True
        return original_run(*args, **kwargs)

    monkeypatch.setattr(role, "run", archive_after_model_starts)
    with pytest.raises(Exception):
        await Scheduler(role, role.store.state("run")["generation"]).decide({"video": None, "events": []})
    assert stopped == [True]


@pytest.mark.asyncio
async def test_mcp_real_stdio_handshake(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from bili_assistant.storage import Store

    data = tmp_path / "mcp-data"
    Store(data).initialize()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "bili_assistant", "--project", str(tmp_path), "--data", str(data), "serve"],
        env={"CODEX_THREAD_ID": ""},
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert "management_open" in [t.name for t in tools.tools]
            assert "assistant_start" in [t.name for t in tools.tools]
            # A missing trusted identity must never be replaced by a model-supplied one.
            result = await client.call_tool("management_open", {})
            assert result.isError
            assert "host_identity_missing" in result.content[0].text
