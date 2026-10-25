"""Isolation configuration and adversarial protocol/lifecycle checks, no live LLM."""
import asyncio
import json
import os
import sys
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from companion_codex.app_server import RpcProcess, TurnEvidence, copy_inputs, verify_configuration
from companion_codex.isolated_artifacts import export_image
from companion_codex.isolation import IsolatedJob, login_params, model_settings
from companion_codex.model import CodexModel
from companion_codex.native import generated_path
from companion_core.errors import AssistantError


def job_at(path, **kwargs):
    return IsolatedJob(path, executable=sys.executable, **kwargs)


def test_default_model_has_no_unsandboxed_fallback():
    assert CodexModel().isolated
    assert not CodexModel(command_factory=lambda *args: []).isolated


@pytest.mark.parametrize("native", [None, "web", "image"])
def test_isolated_configuration_does_not_inherit_user_tools_or_secrets(tmp_path, monkeypatch, native):
    for key in ("OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "DATABASE_URL", "CODEX_THREAD_ID", "HTTP_PROXY"):
        monkeypatch.setenv(key, "SYNTHETIC_SECRET")
    with job_at(tmp_path, native=native, settings={"model": "chosen-model", "notify": "untrusted"}) as job:
        settings = tomllib.loads((job.home / "config.toml").read_text())
        assert settings["cli_auth_credentials_store"] == "ephemeral"
        assert settings["model"] == "chosen-model" and "notify" not in settings
        assert settings["web_search"] == ("live" if native == "web" else "disabled")
        assert all(not value for key, value in settings["features"].items() if key != "image_generation")
        assert settings["features"]["image_generation"] is (native == "image")
        verify_configuration(settings, native)
        with pytest.raises(AssistantError):
            verify_configuration({**settings, "mcp_servers": {"unexpected": {}}}, native)
        assert "SYNTHETIC_SECRET" not in json.dumps(job.environment())
        assert job.environment()["CODEX_HOME"] == str(job.home)
        command = job.command()
        # Codex's sandbox subcommand must own all -c overrides (root overrides
        # are lost in this CLI when the subcommand has another -c).
        assert command[:2] == [str(Path(sys.executable).resolve()), "sandbox"]
        assert 'windows.sandbox="mxc"' in command
        assert "--include-managed-config" in command
        fs = tomllib.loads(next(s for s in command if s.startswith("permissions.comp")))
        assert fs["permissions"]["companion-isolated"]["filesystem"][":root"] == "deny"
    assert not job.root.exists()


def test_credentials_only_return_access_token_and_account_id(tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": "wrong",
        "tokens": {"access_token": "temporary", "account_id": "account", "refresh_token": "NEVER_CHILD"},
    }))
    assert login_params(tmp_path) == {"type": "chatgptAuthTokens", "accessToken": "temporary",
                                      "chatgptAccountId": "account"}
    (tmp_path / "auth.json").write_text('{"secret":"DO_NOT_LOG"}')
    with pytest.raises(AssistantError) as exc:
        login_params(tmp_path)
    assert exc.value.code == "model_auth" and "DO_NOT_LOG" not in str(exc.value)


def test_configuration_does_not_silently_change_provider(tmp_path):
    (tmp_path / "config.toml").write_text('model_provider="custom"')
    with pytest.raises(AssistantError, match="官方"):
        model_settings(tmp_path)


def test_explicit_images_are_copied_without_metadata_or_original_name(tmp_path):
    source = tmp_path / "private-person-name.png"
    Image.new("RGB", (20, 20), "red").save(source)
    with job_at(tmp_path / "jobs") as job:
        copied = copy_inputs(job, [source])
        assert copied == [{"type": "localImage", "path": str(job.work / "input-0.png")}]
        assert Image.open(copied[0]["path"]).size == (20, 20)
        with pytest.raises(AssistantError):
            copy_inputs(job, [source] * 13)


def fake_rpc(tmp_path, source, *, guard=lambda: None, seconds=2, max_bytes=100_000):
    helper = tmp_path / "rpc.py"
    helper.write_text(source, encoding="utf-8")
    return RpcProcess([sys.executable, "-I", str(helper)], cwd=tmp_path, env=dict(os.environ),
                      guard=guard, deadline=time.monotonic() + seconds, max_bytes=max_bytes)


async def test_nonreading_transport_times_out_and_reaps(tmp_path):
    rpc = fake_rpc(tmp_path, "import time; time.sleep(60)", seconds=.3)
    try:
        with pytest.raises(AssistantError) as exc:
            await rpc.call("test", {"text": "x" * 1_000_000})
        assert exc.value.code == "model_timeout"
    finally:
        await asyncio.to_thread(rpc.close)
    assert rpc.process.poll() is not None
    assert not any(thread.is_alive() for thread in rpc.threads)


async def test_cancel_and_pause_reap_transport(tmp_path):
    rpc = fake_rpc(tmp_path, "import time; time.sleep(60)")
    task = asyncio.create_task(rpc.call("test", {}))
    await asyncio.sleep(.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.to_thread(rpc.close)
    assert rpc.process.poll() is not None
    assert not any(thread.is_alive() for thread in rpc.threads)


async def test_protocol_does_not_forward_tool_approval_requests(tmp_path):
    rpc = fake_rpc(tmp_path, '''import sys,json,time
row=json.loads(sys.stdin.readline())
print(json.dumps({'id': 900, 'method': 'item/commandExecution/requestApproval', 'params': {}}), flush=True)
time.sleep(60)
''')
    try:
        with pytest.raises(AssistantError) as exc:
            await rpc.call("thread/start", {})
        assert exc.value.code == "model_tool_denied"
    finally:
        await asyncio.to_thread(rpc.close)


async def test_protocol_size_limit_stops_unterminated_output(tmp_path):
    rpc = fake_rpc(tmp_path, "import sys,time; sys.stdout.write('x'*2000); sys.stdout.flush(); time.sleep(60)",
                   max_bytes=1000)
    try:
        with pytest.raises(AssistantError) as exc:
            await rpc.call("initialize", {})
        assert exc.value.code == "model_output_size"
    finally:
        await asyncio.to_thread(rpc.close)


async def test_small_event_burst_uses_bounded_backpressure_and_batched_guards(tmp_path):
    calls = []

    def slow_guard():
        calls.append(time.monotonic())
        time.sleep(.01)

    rpc = fake_rpc(tmp_path, '''import sys,json,time
request=json.loads(sys.stdin.readline())
for i in range(400):
 print(json.dumps({'method':'synthetic/delta','params':{'index':i}}),flush=True)
print(json.dumps({'id':request['id'],'result':{'ok':True}}),flush=True)
time.sleep(60)
''', guard=slow_guard, seconds=4)
    seen = []
    try:
        assert await rpc.call('synthetic', {}, on_event=seen.append) == {'ok': True}
        assert [event['params']['index'] for event in seen] == list(range(400))
        assert len(calls) < 20 and rpc.incoming.maxsize == 128
        assert not rpc.failed.is_set()
    finally:
        await asyncio.to_thread(rpc.close)


async def test_idle_receive_still_detects_revocation_promptly(tmp_path):
    revoked = False

    def guard():
        if revoked:
            raise AssistantError('synthetic_revoked', 'synthetic')

    rpc = fake_rpc(tmp_path, 'import time; time.sleep(60)', guard=guard)
    task = asyncio.create_task(rpc.receive())
    try:
        await asyncio.sleep(.03)
        revoked = True
        with pytest.raises(AssistantError) as error:
            await asyncio.wait_for(task, .5)
        assert error.value.code == 'synthetic_revoked'
    finally:
        await asyncio.to_thread(rpc.close)


@pytest.mark.parametrize("operation", ["receive", "call"])
async def test_slow_host_guard_leaves_event_loop_responsive(tmp_path, operation):
    rpc = fake_rpc(tmp_path, '''import sys,json,time
print(json.dumps({'method':'synthetic/ready'}),flush=True)
row=json.loads(sys.stdin.readline())
print(json.dumps({'id':row['id'],'result':{'ok':True}}),flush=True)
time.sleep(60)
''', seconds=5)
    entered, release = threading.Event(), threading.Event()

    def slow_guard():
        entered.set()
        if not release.wait(2):
            raise AssistantError("event_loop_blocked", "synthetic")

    rpc.guard = slow_guard
    task = asyncio.create_task(rpc.receive() if operation == "receive" else rpc.call("synthetic", {}))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        # A web request/stop task can run while the host RPC is still waiting.
        assert not task.done()
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result == ({"method": "synthetic/ready"} if operation == "receive" else {"ok": True})
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(rpc.close)


async def test_cancel_during_rpc_construction_collects_and_closes_child():
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    class StartingRpc(RpcProcess):
        def __init__(self):
            entered.set()
            assert release.wait(2)

        def close(self):
            closed.set()

    task = asyncio.create_task(StartingRpc.open())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_evidence_rejects_tools_and_cross_thread_events():
    evidence = TurnEvidence()
    evidence.thread_id = "own"
    for params in ({"threadId": "other"}, {"threadId": "own", "item": {"type": "commandExecution"}}):
        with pytest.raises(AssistantError):
            evidence.update({"method": "item/started", "params": params})


def test_process_warning_is_not_cross_thread_output_or_result_evidence():
    evidence = TurnEvidence()
    evidence.thread_id, evidence.turn_id = "own", "turn"
    evidence.update({"method": "warning", "params": {"threadId": None, "message": "synthetic"}})
    assert evidence.answer == "" and not evidence.completed and not evidence.items
    with pytest.raises(AssistantError):
        evidence.update({"method": "warning", "params": {"threadId": "other", "message": "synthetic"}})
    with pytest.raises(AssistantError):
        evidence.update({"method": "item/completed", "params": {"threadId": None, "item": {
            "type": "agentMessage", "text": "forged result"}}})


def test_web_sources_require_tool_results_and_six_call_bound():
    evidence = TurnEvidence(native="web")
    for index in range(6):
        evidence.update({"method": "item/completed", "params": {"item": {
            "id": str(index), "type": "webSearch", "action": {"type": "open_page"},
            "results": [{"url": "https://example.com/page", "ref_id": "ref"}],
        }}})
    assert evidence.trace["opened"] == [{"url": "https://example.com/page", "reference": "ref"}]
    with pytest.raises(AssistantError):
        evidence.update({"method": "item/completed", "params": {"item": {
            "id": "extra", "type": "webSearch", "results": [],
        }}})


def test_generated_image_is_exported_and_tamper_evident(tmp_path):
    with job_at(tmp_path / "jobs") as job:
        evidence = TurnEvidence(native="image")
        evidence.thread_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        root = job.home / "generated_images" / evidence.thread_id
        root.mkdir(parents=True)
        image = root / "result.png"
        Image.new("RGB", (20, 20), "red").save(image)
        decision = {"image_path": str(image)}
        export_image(job, evidence, decision, tmp_path)
        path = generated_path(decision["image_path"], evidence.trace)
        assert path.is_file() and not path.is_relative_to(job.root)
        path.write_bytes(b"tampered")
        with pytest.raises(AssistantError):
            generated_path(str(path), evidence.trace)
        decision["image_path"] = str(tmp_path / "unrelated.png")
        with pytest.raises(AssistantError):
            export_image(job, evidence, decision, tmp_path)


async def test_default_model_uses_isolated_protocol(tmp_path, monkeypatch):
    called = []

    async def generate(*args, **kwargs):
        called.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr("companion_codex.app_server.generate", generate)
    result = await CodexModel().generate("instruction", {}, {}, store=SimpleNamespace(runtime=tmp_path),
                                         policy={}, guard=lambda: None)
    assert result == {"ok": True} and len(called) == 1
    assert called[0]["reasoning_effort"] is None


async def test_explicit_effort_stays_in_isolated_request(tmp_path, monkeypatch):
    called = []

    async def generate(*args, **kwargs):
        called.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr("companion_codex.app_server.generate", generate)
    await CodexModel(reasoning_effort="low", model="synthetic-model").generate("instruction", {}, {},
        store=SimpleNamespace(runtime=tmp_path), policy={}, guard=lambda: None)
    assert called[0]["reasoning_effort"] == "low"
    assert called[0]["model"] == "synthetic-model"
    for kwargs in ({"reasoning_effort": "invalid"},
                   {"reasoning_effort": "low", "command_factory": lambda: None},
                   {"model": "bad\nmodel"}, {"model": "synthetic", "command_factory": lambda: None}):
        with pytest.raises(AssistantError) as error:
            CodexModel(**kwargs)
        assert error.value.code == "model_config"
