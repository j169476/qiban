"""Behavioral checks for independent comment likes, source handoff and illustrated publication."""

import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from bili_assistant.api import call
from bili_assistant.errors import AssistantError
from bili_assistant.publication import import_image, post_once, resolve, send_images
from bili_assistant.research import capture, export, save_note


def event(role, event_id="reply-1", kind="reply", uid="888"):
    body = {"id": event_id, "kind": kind, "uid": uid, "oid": 100, "root": 101, "parent": 202,
            "received": role.clock().isoformat(), "text": "测试评论"}
    with role.store.db() as db:
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (event_id, "123", uid, kind,
                   body["received"], json.dumps(body)))
    return body


@pytest.mark.asyncio
async def test_comment_like_has_own_quota_and_unknown_results_are_not_retried(role):
    event(role)
    policy = role.store.read("policy")
    policy["data"]["actions"]["comment_like"] = {"enabled": True, "daily_limit": 1}
    policy["data"]["actions"]["like"]["enabled"] = False
    role.store.write("policy", policy["data"], policy["version"], "test")
    action = role.prepare("comment_like", {"event_id": "reply-1"})
    role.adapter.fail = True
    assert (await role.execute(action["id"]))["status"] == "uncertain"
    assert role.prepare("comment_like", {"event_id": "reply-1"}, retry_failed=True)["duplicate"]
    event(role, "reply-2")
    another = role.prepare("comment_like", {"event_id": "reply-2"})
    with pytest.raises(AssistantError, match="额度"):
        await role.execute(another["id"])
    assert len(role.adapter.sent) == 1
    assert not role.store.records("conversation_turns")[0]


@pytest.mark.asyncio
async def test_comment_like_requires_real_comment_and_respects_old_policy_and_pause(role):
    event(role, "dm-1", "dm_reply")
    action = role.prepare("comment_like", {"event_id": "dm-1"})
    with pytest.raises(AssistantError, match="实际收到"):
        await role.execute(action["id"])
    event(role)
    action = role.prepare("comment_like", {"event_id": "reply-1"})
    policy = role.store.read("policy")
    del policy["data"]["actions"]["comment_like"]
    role.store.write("policy", policy["data"], policy["version"], "test")
    with pytest.raises(AssistantError, match="已关闭"):
        await role.execute(action["id"])
    role.stop(paused=True)
    with pytest.raises(AssistantError):
        await role.execute(action["id"])
    assert not role.adapter.sent


@pytest.mark.asyncio
async def test_comment_like_targets_actual_parent_rpid(store, monkeypatch):
    from bilibili_api import Credential

    from bili_assistant import credentials, publication
    from bili_assistant.bilibili import Bilibili

    monkeypatch.setattr(credentials, "load", lambda _: Credential(sessdata="fake", bili_jct="fake"))
    sent = []
    async def send(api, guard):
        guard()
        sent.append(api.data)
        return {}
    monkeypatch.setattr(publication, "post_once", send)
    result = await Bilibili(store).perform("comment_like", {"_event": {"oid": 100, "parent": 202, "root": 101}}, {}, lambda: None)
    assert sent[0]["rpid"] == 202 and sent[0]["oid"] == 100 and sent[0]["type"] == 1
    assert sent[0]["action"] == 1 and result == {"rpid": "202"}


def web_note(role):
    source = capture(role, "https://example.org/method", "官方方法说明", "每次只改变一个参数，再观察结果。", "native-web:fixture")
    note = save_note(role, source["data"]["id"], source["version"], "控制变量有助于定位影响，但不能排除变量交互。",
                     ["每次只改变一个参数"], ["调试"])
    return source, note


@pytest.mark.asyncio
async def test_web_review_does_not_fetch_empty_bvid_and_cannot_borrow_other_page_evidence(role, monkeypatch):
    from test_cognition import configure
    from test_knowledge import payload_for, result_for

    from bili_assistant.knowledge import maintain, save_review, source_excerpt
    from bili_assistant.scheduler import Scheduler

    configure(role, knowledge_review=True)
    first, note = web_note(role)
    scheduler = Scheduler(role, role.run()["generation"])
    async def no_bili(*args, **kwargs):
        raise AssertionError("Web reviews must not use the Bilibili video API")
    async def model(prompt, payload, schema, images=()):
        return result_for(payload, [first])
    monkeypatch.setattr(role, "video_context", no_bili)
    monkeypatch.setattr(scheduler, "model", model)
    assert (await maintain(scheduler))["type"] == "knowledge_review"
    second = capture(role, "https://example.org/other", "另一来源", "不相同的适用条件。", "web:other")
    other = save_note(role, second["data"]["id"], second["version"], "方法有不同适用条件。", ["不相同的适用条件"], [])
    job = {"target": note, "related": [other]}
    sources = [first, second]
    result = result_for(job, sources)
    result["claims"][0]["evidence"] = [{"source_id": second["data"]["id"], "quote": "不相同的适用条件"}]
    with pytest.raises(AssistantError, match="该笔记"):
        save_review(role, job, sources, payload_for(job, sources), result)
    long = capture(role, "https://example.org/long", "长段落", "开头" * 2000 + "关键条件" + "结尾" * 2000, "web:long")
    excerpt = source_excerpt(long, "关键条件", 500)
    assert excerpt["truncated_segments"] == 1 and "关键条件" in excerpt["segments"][0]["text"]
    assert excerpt["segments"][0]["text"] in long["data"]["segments"][0]["text"]


def test_web_source_versions_quotes_and_role_separation(role, admin):
    source, note = web_note(role)
    assert note["data"]["coverage"]["kind"] == "web"
    with pytest.raises(AssistantError, match="逐字"):
        save_note(role, source["data"]["id"], source["version"], "凭空断言", ["不存在的原句"], [])
    with pytest.raises(AssistantError):
        capture(admin, "https://example.org/", "title", "text", "ref")
    source["data"]["segments"][0]["text"] = "本地修正后的材料"
    role.store.write("source_snapshots", source["data"], source["version"], "user", source["data"]["id"])
    with pytest.raises(AssistantError, match="已变化"):
        save_note(role, source["data"]["id"], source["version"], "old", ["原句"], [])
    report = export(role, "报告", [note["data"]["id"]])
    assert report["stale_knowledge_ids"] == [note["data"]["id"]]
    assert "待重新核验" in Path(report["path"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("format", ["markdown", "html", "docx", "mindmap"])
def test_reports_are_portable_and_keep_sources(role, format):
    _, note = web_note(role)
    report = export(role, "<script>alert(1)</script>", [note["data"]["id"]], format, "cards")
    path = Path(report["path"])
    assert path.is_file() and path.with_suffix(".json").is_file()
    assert len(report["references"]) == 2 and not report["stale_knowledge_ids"]
    if format == "html":
        text = path.read_text(encoding="utf-8")
        assert "<script>" not in text and "&lt;script&gt;" in text
        assert "https://example.org/method" in text
    elif format == "docx":
        from docx import Document

        text = "\n".join(p.text for p in Document(path).paragraphs)
        assert "https://example.org/method" in text and "控制变量" in text
    elif format == "mindmap":
        assert ET.parse(path).getroot().tag == "map"


def picture(role, tmp_path):
    path = tmp_path / "native-generated.png"
    Image.new("RGB", (32, 24), "navy").save(path)
    record = import_image(role, str(path), "codex_imagegen", "已审阅的配图")
    return {"id": record["data"]["id"], "version": record["version"]}


def test_content_backup_keeps_registered_images_but_excludes_models(role, tmp_path):
    reference = picture(role, tmp_path)
    path = resolve(role.store, [reference])[0]["path"]
    models = role.store.root / "models"
    models.mkdir()
    (models / "ignored.bin").write_bytes(b"weights")
    result = role.store.export(tmp_path / "backup.zip")
    assert not result["missing_assets"]
    with zipfile.ZipFile(result["path"]) as backup:
        assert Path(path).relative_to(role.store.root).as_posix() in backup.namelist()
        assert all(not n.startswith(("models/", "runtime/")) for n in backup.namelist())


@pytest.mark.asyncio
async def test_publication_images_are_frozen_and_read_only_blocks_upload(role, tmp_path):
    reference = picture(role, tmp_path)
    assets = resolve(role.store, [reference])
    action = role.prepare("dynamic", {"text": "测试配图", "images": [reference]})
    Image.new("RGB", (32, 24), "red").save(assets[0]["path"])
    with pytest.raises(AssistantError, match="素材已改变"):
        await role.execute(action["id"])
    assert not role.adapter.sent
    run = role.store.state("run")
    run["read_only"] = True
    role.store.set_state("run", run)
    with pytest.raises(AssistantError, match="只读"):
        role.prepare("dynamic", {"text": "test", "images": [reference]})


@pytest.mark.asyncio
async def test_guard_runs_after_sdk_preparation_without_post_retry(monkeypatch):
    from bilibili_api.utils import network

    sent = []
    class Api:
        async def _prepare_request(self):
            sent.append("prepare")
            return {"method": "POST"}
        def _process_response(self, response):
            return response
    class Client:
        async def request(self, **kwargs):
            sent.append("POST")
            raise TimeoutError()
    monkeypatch.setattr(network, "get_client", Client)
    with pytest.raises(TimeoutError):
        await post_once(Api(), lambda: sent.append("guard"))
    assert sent == ["prepare", "guard", "POST"]
    sent.clear()
    def stopped():
        raise AssistantError("before_send_cancelled", "stopped")
    with pytest.raises(AssistantError):
        await post_once(Api(), stopped)
    assert sent == ["prepare"]


@pytest.mark.asyncio
async def test_archive_after_upload_prevents_final_dynamic_and_records_partial(role, tmp_path, monkeypatch):
    from bilibili_api import Credential

    from bili_assistant import publication

    reference = picture(role, tmp_path)
    calls = []
    class Content:
        async def get_contents(self, credential):
            role.host.states["role"] = True
            return []
    async def post(api, guard):
        guard()
        calls.append(api)
        return {"image_url": "https://i0.hdslb.com/test.png", "image_width": 32, "image_height": 24}
    monkeypatch.setattr(publication, "post_once", post)
    def guard():
        try:
            role.role_run()
        except AssistantError:
            raise AssistantError("before_send_cancelled", "stopped") from None
    with pytest.raises(AssistantError) as exc:
        await send_images(role.store, Content(), [reference], Credential(sessdata="fake", bili_jct="fake"), guard)
    assert exc.value.code == "publication_partial" and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_illustrated_dynamic_requires_platform_identifier(store, monkeypatch, confirmed):
    from bilibili_api import Credential

    from bili_assistant import credentials, publication
    from bili_assistant.bilibili import Bilibili

    monkeypatch.setattr(credentials, "load", lambda _: Credential(sessdata="fake", bili_jct="fake"))
    async def send(*args):
        args[-1]()
        return {"dyn_id_str": "333"} if confirmed else {}
    monkeypatch.setattr(publication, "send_images", send)
    operation = Bilibili(store).perform("dynamic", {"text": "配图正文", "images": [{"id": "test", "version": "test"}]}, {}, lambda: None)
    if confirmed:
        assert (await operation)["dynamic_id_str"] == "333"
    else:
        with pytest.raises(AssistantError, match="可核对 ID"):
            await operation


@pytest.mark.asyncio
async def test_tools_validate_new_contracts_without_starting_role(role):
    role.stop(paused=True)
    source = await call(role, "web_source_capture", {"url": "https://example.org", "title": "来源",
                       "excerpt": "真实摘录", "tool_reference": "web:fixture"})
    assert source["data"]["capture"]["scope"] == "excerpt"
    assert role.store.state("run")["status"] == "paused"
