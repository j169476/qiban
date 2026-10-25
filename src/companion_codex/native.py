"""Codex native-tool event evidence and command profiles."""
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from companion_core.errors import AssistantError


def public_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password
                or any(ord(c) < 33 for c in value)):
            raise ValueError()
        return parsed._replace(fragment="").geturl()
    except ValueError:
        raise AssistantError("web_url", "网页来源需要不含账号密码的 HTTP(S) URL") from None


def command_profile(command, kind):
    if kind not in ("web", "image"):
        raise AssistantError("native_kind", "不支持的原生工具任务")
    command = list(command)
    if "--json" not in command:
        command[-1:-1] = ["--json"]
    if kind == "web":
        command[command.index('web_search="disabled"')] = 'web_search="live"'
    else:
        command[command.index("image_generation") - 1] = "--enable"
    return command


def evidence(path, started):
    """Keep URLs of opened pages, not arbitrary URLs supplied by the final model answer."""

    if path.stat().st_size > 8_000_000:
        raise AssistantError("model_output_size", "原生工具事件超出本轮上限")
    result = {"thread_id": "", "opened": [], "started": started, "web_calls": 0}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "thread.started":
            value = event.get("thread_id", "")
            if re.fullmatch(r"[a-f0-9-]{36}", value):
                result["thread_id"] = value
        item = event.get("item", {})
        if event.get("type") != "item.completed" or item.get("type") != "web_search":
            continue
        result["web_calls"] += 1
        if item.get("action", {}).get("type") != "open_page":
            continue
        for source in item.get("results", []):
            try:
                url = public_url(source.get("url", ""))
            except AssistantError:
                continue
            if source.get("ref_id") and url not in [x["url"] for x in result["opened"]]:
                result["opened"].append({"url": url, "reference": source["ref_id"]})
    return result


def generated_path(value, trace):
    """CLI currently omits image tool events; accept only new files in this exact session."""
    if proof := trace.get("isolated_image"):
        path = Path(value)
        if (str(path) != proof.get("path") or path.is_symlink() or not path.is_file()
                or path.stat().st_size > 20_000_000
                or hashlib.sha256(path.read_bytes()).hexdigest() != proof.get("sha256")):
            raise AssistantError("native_image_evidence", "隔离图片导出证据不一致")
        return path
    thread = trace.get("thread_id", "")
    if not re.fullmatch(r"[a-f0-9-]{36}", thread):
        raise AssistantError("native_image_evidence", "缺少本轮 Codex 会话证据")
    root = (Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "generated_images" / thread).resolve()
    path = Path(value)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.resolve().parent != root or path.stat().st_mtime < trace["started"] - 2):
        raise AssistantError("native_image_evidence", "图片不属于本次原生生成任务")
    return path
