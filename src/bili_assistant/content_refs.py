"""Platform content references; derived links never become identity evidence."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from .errors import AssistantError

VIDEO_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
URL_PATTERN = re.compile(
    r"https?://[^\s<>\"'\u3000]+|//(?:www\.|m\.)?bilibili\.com/[^\s<>\"']+"
    r"|(?<![A-Za-z0-9.])b23\.tv/[A-Za-z0-9]{1,32}(?:\?[^\s<>\"']*)?", re.I)
BV_PATTERN = re.compile(r"(?<![A-Za-z0-9])BV[A-Za-z0-9]{10}(?![A-Za-z0-9])")


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 for c in value):
        return None
    value = html.unescape(value)
    if value.startswith("//"):
        value = "https:" + value
    elif value.lower().startswith("b23.tv/"):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"https", "http"} or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443) or parsed.hostname not in VIDEO_HOSTS | {"b23.tv"}
                or "\\" in value):
            return None
    except ValueError:
        return None
    return parsed._replace(scheme="https", netloc=parsed.hostname, fragment="").geturl()


def parse_video(value):
    """Parse an exact video URL or bare BV ID without performing I/O."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if BV_PATTERN.fullmatch(value):
        return {"bvid": value, "page_index": 0, "source_url": value,
                "resolved_url": f"https://www.bilibili.com/video/{value}?p=1"}
    url = safe_url(value)
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.hostname not in VIDEO_HOSTS:
        return None
    match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10}|av[0-9]{1,20})/?", parsed.path)
    if not match:
        return None
    pages = parse_qs(parsed.query, keep_blank_values=True).get("p", ["1"])
    if len(pages) != 1 or not re.fullmatch(r"[0-9]{1,5}", pages[0]) or not 1 <= int(pages[0]) <= 10000:
        return None
    bvid = match[1]
    if bvid.startswith("av"):
        from bilibili_api.utils.aid_bvid_transformer import aid2bvid

        try:
            if int(bvid[2:]) <= 0:
                return None
            bvid = aid2bvid(int(bvid[2:]))
        except (ValueError, TypeError, OverflowError):
            return None
    page = int(pages[0])
    return {"bvid": bvid, "page_index": page - 1, "source_url": value,
            "resolved_url": f"https://www.bilibili.com/video/{bvid}?p={page}"}


def references(text):
    """Only recognized site URLs, then bare IDs outside URL text, are candidates."""
    text = str(text)[:16000]
    urls = [match[0].rstrip(".,;:!?，。；：！？）)]}>") for match in URL_PATTERN.finditer(text)]
    found = [url for url in urls if safe_url(url)]
    without_urls = URL_PATTERN.sub(" ", text)
    found.extend(match[0] for match in BV_PATTERN.finditer(without_urls))
    return list(dict.fromkeys(found))[:6]


def event_video(event):
    text_refs = references(event.get("text", ""))
    # An actual share card's page is stronger than a bare BV-shaped string in
    # its title. Explicit video URLs in ordinary text remain first.
    candidates = [value for value in text_refs if not BV_PATTERN.fullmatch(value)]
    candidates += [a.get("url", "") for a in event.get("attachments", []) if a.get("kind") != "image"]
    candidates += [value for value in text_refs if BV_PATTERN.fullmatch(value)]
    for value in candidates:
        if result := parse_video(value):
            return result
    if any((url := safe_url(value)) and urlsplit(url).hostname == "b23.tv" for value in candidates):
        # An explicit unresolved invitation names another resource. The video
        # hosting its comment is only a fallback when no such link is present.
        return None
    bvid = event.get("bvid", "")
    if BV_PATTERN.fullmatch(str(bvid)):
        page = event.get("page_index", 0)
        if type(page) is int and 0 <= page < 10000:
            return parse_video(f"https://www.bilibili.com/video/{bvid}?p={page + 1}")
    return None


def source_hash(event):
    return hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cached_invitation(store, account, event):
    cached = store.state(f"invitation-reference:{account}:{event['id']}") or {}
    return cached.get("reference") if cached.get("source_hash") == source_hash(event) else None


async def resolve_short(value, guard=lambda: None):
    """Visit only b23.tv; validate each Location before any subsequent request."""
    url = safe_url(value)
    if not url or urlsplit(url).hostname != "b23.tv" or not re.fullmatch(r"/[A-Za-z0-9]{1,32}/?", urlsplit(url).path):
        raise AssistantError("short_link_scope", "只展开 B站 b23.tv 短链接")

    async def resolve():
        visited = set()
        current = url
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            for _ in range(3):
                guard()
                if current in visited:
                    raise AssistantError("short_link_loop", "短链接循环跳转")
                visited.add(current)
                async with client.stream("HEAD", current) as response:
                    status, location = response.status_code, response.headers.get("Location")
                if status == 405:
                    guard()
                    # Read headers only; never load an arbitrary HTML response.
                    async with client.stream("GET", current) as response:
                        status, location = response.status_code, response.headers.get("Location")
                guard()
                if status not in {301, 302, 303, 307, 308} or not location:
                    raise AssistantError("short_link_unresolved", "短链接没有可验证的视频跳转")
                target = safe_url(urljoin(current, location))
                if not target:
                    raise AssistantError("short_link_scope", "短链接跳转目标不在允许范围")
                if reference := parse_video(target):
                    return {**reference, "source_url": value}
                parsed = urlsplit(target)
                if parsed.hostname != "b23.tv" or not re.fullmatch(r"/[A-Za-z0-9]{1,32}/?", parsed.path):
                    raise AssistantError("short_link_scope", "短链接最终目标不是受支持的 B站视频")
                current = target
        raise AssistantError("short_link_hops", "短链接跳转超过三次")

    try:
        return await asyncio.wait_for(resolve(), timeout=10)
    except (httpx.HTTPError, TimeoutError):
        raise AssistantError("short_link_unavailable", "短链接当前无法解析") from None


async def resolve_invitation(engine, event):
    """Resolve only a bound account's actual guide event, keeping raw evidence intact."""
    run = engine.role_run()
    account, event_id = run["account"], event.get("id", "")
    with engine.store.db() as db:
        row = db.execute("SELECT body FROM events WHERE account=? AND id=?", (account, event_id)).fetchone()
    if not row:
        raise AssistantError("unknown_event", "视频邀请必须来自实际收到的消息")
    original = engine.store.ledger_decode("events.body", row["body"])
    guide = engine.store.read("companion")["data"].get("guide_uid")
    if not guide or original["uid"] != guide:
        return dict(event)
    before = source_hash(original)

    def guard():
        engine.run(run["generation"])
        if engine.store.read("companion")["data"].get("guide_uid") != guide:
            raise AssistantError("context_changed", "视频邀请来源配置已变化")
        with engine.store.db() as db:
            latest = db.execute("SELECT body FROM events WHERE account=? AND id=?", (account, event_id)).fetchone()
        if not latest or source_hash(engine.store.ledger_decode("events.body", latest["body"])) != before:
            raise AssistantError("context_changed", "视频邀请原文已变化")

    reference = cached_invitation(engine.store, account, original) or event_video(original)
    if reference is None:
        values = references(original.get("text", "")) + [a.get("url", "") for a in original.get("attachments", [])]
        short = next((safe_url(value) for value in values if safe_url(value)
                      and urlsplit(safe_url(value)).hostname == "b23.tv"), None)
        if short:
            try:
                reference = await resolve_short(short, guard)
            except AssistantError as exc:
                if not exc.code.startswith("short_link_"):
                    raise
                return {**event, "bvid": "", "page_index": 0, "video_reference_error": exc.code}
    guard()
    if reference is None:
        return dict(event)
    engine.store.set_state(f"invitation-reference:{account}:{event_id}",
                           {"source_hash": before, "reference": reference})
    return {**event, "bvid": reference["bvid"], "page_index": reference["page_index"], "video_reference": reference}
