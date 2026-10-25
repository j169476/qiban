import json
import time
from pathlib import Path

import pytest
from PIL import Image

from bili_assistant import native_tools as native
from bili_assistant.errors import AssistantError
from bili_assistant.scheduler import Scheduler, worker_command


class NativeScheduler(Scheduler):
    def __init__(self, role, response, callback=None):
        super().__init__(role, role.store.state("run")["generation"])
        self.response, self.callback, self.calls = response, callback, []

    async def model(self, prompt, context, schema, images=(), **kwargs):
        self.calls.append((schema, images, kwargs))
        if self.callback:
            self.callback()
        if kwargs.get("guard"):
            kwargs["guard"]()
        if schema is native.REVIEW:
            return {"approved": True, "description": "蓝色圆形", "reason": "符合正文"}
        return self.response


def web_response(opened=True):
    return {"result": {"status": "done", "reason": "", "sources": [{
        "url": "https://docs.python.org/3/library/json.html#json.loads", "title": "JSON",
        "excerpt": "Deserialize a JSON document.", "note": "JSON 可以解码为 Python 对象。", "tags": ["Python"],
    }], "image_path": ""}, "evidence": {"thread_id": "11111111-1111-1111-1111-111111111111",
        "opened": [{"url": "https://docs.python.org/3/library/json.html", "reference": "turn0view0"}] if opened else []}}


def job(role, record):
    return role.store.read("native_jobs", record["data"]["id"])


def test_native_profiles_keep_other_tools_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr("bili_assistant.scheduler.shutil.which", lambda _: str(tmp_path / "codex.exe"))
    base = worker_command(tmp_path, tmp_path / "out", tmp_path / "schema")
    web = native.command_profile(base, "web")
    image = native.command_profile(base, "image")
    assert 'web_search="disabled"' in base and 'web_search="live"' in web
    assert image[image.index("image_generation") - 1] == "--enable"
    for command in (web, image):
        for feature in ("shell_tool", "plugins", "apps", "multi_agent"):
            assert command[command.index(feature) - 1] == "--disable"
        assert "read-only" in command and "--json" in command


def test_only_opened_web_events_count(tmp_path):
    path = tmp_path / "trace"
    event = {"type": "item.completed", "item": {"type": "web_search", "action": {"type": "search"},
             "results": [{"url": "https://example.com", "ref_id": "turn0search0"}]}}
    path.write_text(json.dumps(event), encoding="utf-8")
    assert native.evidence(path, 0)["opened"] == []
    event["item"]["action"]["type"] = "open_page"
    path.write_text(json.dumps(event), encoding="utf-8")
    assert native.evidence(path, 0)["opened"][0]["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_web_job_deduplicates_and_saves_actual_source_links(role):
    record = native.create(role, "web", "JSON 如何解码？")
    assert native.create(role, "web", "JSON 如何解码？")["duplicate"]
    scheduler = NativeScheduler(role, web_response())
    assert await native.step(scheduler)
    assert job(role, record)["data"]["status"] == "completed"
    notes = role.store.records("knowledge")[0]
    assert len(notes) == 1 and notes[0]["data"]["source_ref"]
    assert "not_independently_fetched" in job(role, record)["data"]["result"]["evidence_scope"]
    assert not await native.step(scheduler)
    assert len(scheduler.calls) == 1 and not role.adapter.sent
    assert native.status(role)["used_today"]["web"] == 1


@pytest.mark.asyncio
async def test_fabricated_web_source_is_rejected(role):
    record = native.create(role, "web", "查一个问题")
    await native.step(NativeScheduler(role, web_response(False)))
    assert job(role, record)["data"]["error"] == "native_web_evidence"
    assert not role.store.records("knowledge")[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancel", "archive", "policy"])
async def test_changes_during_native_call_discard_result(role, change):
    record = native.create(role, "web", "查一个问题")
    def mutate():
        if change == "cancel":
            current = job(role, record)
            native.control(role, current["data"]["id"], "cancel", current["version"])
        elif change == "archive":
            role.host.states["role"] = True
        else:
            current = role.store.read("policy")
            current["data"]["daily_model_cycles"] += 1
            role.store.write("policy", current["data"], current["version"], "test")
    await native.step(NativeScheduler(role, web_response(), mutate))
    assert job(role, record)["data"]["status"] in ("failed", "cancelled")
    assert not role.store.records("source_snapshots")[0]


@pytest.mark.asyncio
async def test_quota_and_interrupted_job_do_not_replay(role):
    record = native.create(role, "web", "问题一")
    doc = record["data"]
    doc.update(status="running", attempts=1)
    role.store.write("native_jobs", doc, record["version"], "test", doc["id"])
    scheduler = NativeScheduler(role, web_response())
    assert not await native.step(scheduler)
    assert job(role, record)["data"]["error"] == "interrupted_no_automatic_retry"
    record = native.create(role, "web", "问题二")
    role.store.set_state(native.usage(role, "web"), 3)
    assert not await native.step(scheduler)
    assert job(role, record)["data"]["status"] == "pending" and not scheduler.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stage", [None, "reservation", "upload"])
async def test_image_is_reviewed_then_published_once(role, tmp_path, monkeypatch, cancel_stage):
    current = role.store.read("policy")
    current["data"]["actions"]["dynamic"] = {"enabled": True, "daily_limit": 2}
    role.store.write("policy", current["data"], current["version"], "test")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    thread = "11111111-1111-1111-1111-111111111111"
    image = tmp_path / "generated_images" / thread / "test.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (16, 16), "blue").save(image)
    response = {"result": {"status": "done", "reason": "", "image_path": str(image), "sources": []},
                "evidence": {"thread_id": thread, "started": time.time(), "opened": []}}
    record = native.create(role, "image", "蓝色圆形", draft="今天练习了圆形。")
    if cancel_stage == "reservation":
        original = role.prepare
        def prepare(*args, **kwargs):
            action = original(*args, **kwargs)
            current = job(role, record)
            native.control(role, record["data"]["id"], "cancel", current["version"])
            return action
        role.prepare = prepare
    if cancel_stage == "upload":
        async def perform(kind, payload, policy, before_send):
            import asyncio

            from conftest import FakeHost

            from bili_assistant.engine import Engine
            from bili_assistant.storage import Store

            other = Engine(Store(role.store.root), FakeHost(), role.adapter, role.clock)
            current = job(role, record)
            result = await asyncio.wait_for(asyncio.to_thread(
                native.control, other, record["data"]["id"], "cancel", current["version"]), 3)
            assert result["cancellation_requested"]
            before_send()
            role.adapter.sent.append(payload)
            return {}
        role.adapter.perform = perform
    scheduler = NativeScheduler(role, response)
    assert await native.step(scheduler)
    result = job(role, record)["data"]
    assert result["status"] == ("cancelled" if cancel_stage else "completed"), result
    assert len(role.adapter.sent) == (0 if cancel_stage else 1)
    assert scheduler.calls[1][1] and Path(scheduler.calls[1][1][0]).is_file()
    assert not await native.step(scheduler)
    assert len(role.adapter.sent) == (0 if cancel_stage else 1)
    with pytest.raises(AssistantError):
        native.control(role, result["id"], "retry", job(role, record)["version"])


def test_other_session_image_or_read_only_publication_rejected(role, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = tmp_path / "unrelated.png"
    Image.new("RGB", (10, 10)).save(path)
    with pytest.raises(AssistantError, match="不属于"):
        native.generated_path(str(path), {"thread_id": "11111111-1111-1111-1111-111111111111", "started": 0})
    run = role.store.state("run")
    run["read_only"] = True
    role.store.set_state("run", run)
    with pytest.raises(AssistantError):
        native.create(role, "image", "一个圆", draft="正文")
