"""Normalize platform cards/images and preserve bounded, explicitly paginated comment context."""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from .content_refs import event_video, parse_video
from .conversations import identifier
from .errors import AssistantError
from .media import download_image, image_url


def bili_url(value):
    if not isinstance(value, str):
        return ""
    value = "https:" + value if value.startswith("//") else value
    try:
        parsed = urlsplit(value)
        if parsed.port not in (None, 80, 443):
            return ""
    except ValueError:
        return ""
    if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
        return ""
    host = parsed.hostname or ""
    if host != "b23.tv" and host != "bilibili.com" and not host.endswith(".bilibili.com"):
        return ""
    return value[:2000]


def video_id(value):
    return match[0] if (match := re.search(r"BV[A-Za-z0-9]{10}", str(value))) else ""


def image_attachment(value):
    try:
        return {"kind": "image", "url": image_url(value), "title": "", "bvid": ""}
    except (AssistantError, ValueError):
        return None


def comment_attachments(content):
    result = []
    pictures = content.get("pictures") or []
    for picture in (pictures if isinstance(pictures, list) else [])[:9]:
        item = image_attachment(picture.get("img_src", "")) if isinstance(picture, dict) else None
        if item:
            result.append(item)
    jumps = content.get("jump_url") or {}
    if isinstance(jumps, dict):
        for key, card in list(jumps.items())[:20]:
            if not isinstance(card, dict):
                continue
            url = bili_url(card.get("url", key))
            if not url:
                continue
            bvid = video_id(url)
            result.append({"kind": "video_card" if bvid else "link_card", "url": url,
                           "title": str(card.get("title", ""))[:500], "bvid": bvid})
    return result[:12]


def private_content(message):
    """SDK EventType: text=1, image=2/6, shared video=7, pushed video=11."""
    try:
        body = json.loads(message["content"]) if isinstance(message["content"], str) else message["content"]
        if not isinstance(body, dict):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    kind = message.get("msg_type")
    if kind == 1:
        text = str(body.get("content", ""))[:16000]
        reference = event_video({"text": text}) or {}
        return {"text": text, "bvid": reference.get("bvid", ""),
                "page_index": reference.get("page_index", 0), "attachments": []}
    if kind in (2, 6):
        item = image_attachment(body.get("url", ""))
        return {"text": "[图片消息]", "bvid": "", "attachments": [item]} if item else None
    if kind in (7, 11):
        bvid = video_id(body.get("bvid", ""))
        if not bvid:
            try:
                from bilibili_api.utils.aid_bvid_transformer import aid2bvid

                aid = int(body.get("id", 0))
                if aid <= 0:
                    return None
                bvid = aid2bvid(aid)
            except (ValueError, TypeError, OverflowError):
                return None
        title = str(body.get("title", ""))[:500]
        reference = next((ref for key in ("url", "uri", "jump_url")
                          if (ref := parse_video(body.get(key))) and ref["bvid"] == bvid), None)
        reference = reference or parse_video(f"https://www.bilibili.com/video/{bvid}")
        card = {"kind": "video_card", "url": f"https://www.bilibili.com/video/{bvid}",
                "title": title, "bvid": bvid}
        card["url"] = reference["resolved_url"]
        return {"text": "[分享视频] " + title, "bvid": bvid, "page_index": reference["page_index"], "attachments": [card]}
    return None


def normalize_comment(row):
    if not isinstance(row, dict):
        return None
    member, content = row.get("member") or {}, row.get("content") or {}
    if not isinstance(member, dict) or not isinstance(content, dict):
        return None
    rpid, uid = str(row.get("rpid_str") or row.get("rpid") or ""), str(row.get("mid_str") or row.get("mid") or "")
    if not re.fullmatch(r"[0-9]{1,20}", rpid) or not re.fullmatch(r"[0-9]{1,20}", uid):
        return None
    stamp = None
    try:
        stamp = datetime.fromtimestamp(float(row["ctime"]), timezone.utc).isoformat()
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        pass
    return {
        "rpid": rpid, "uid": uid, "display_name": str(member.get("uname", ""))[:120],
        "root": str(row.get("root_str") or row.get("root") or ""),
        "parent": str(row.get("parent_str") or row.get("parent") or ""),
        "text": str(content.get("message", ""))[:6000], "received": stamp,
        "text_truncated": len(str(content.get("message", ""))) > 6000,
        "attachments": comment_attachments(content),
    }


def comment_page_id(account, oid, root, page, resource_type=1):
    parts = (account, str(oid), str(root), page)
    return identifier("comments", *parts) if resource_type == 1 else identifier("comments", resource_type, *parts)


def save_page(engine, oid, root, page, data, crawl_id, expected=None, *, resource_type=1):
    store = engine.store
    account = store.read("manifest")["data"]["account_uid"]
    item_id = comment_page_id(account, oid, root, page, resource_type)
    with store.lock:
        try:
            old = store.read("comment_pages", item_id)
        except AssistantError as exc:
            if exc.code != "not_found":
                raise
            old = None
        last_written = store.state("comment-page-written:" + item_id)
        if old and (old["data"]["locked"] or old["version"] != last_written):
            return {**old, "manual_override": True}
        if old and old["version"] != expected:
            raise AssistantError("context_changed", "读取评论期间本地分页记录被修改，请重读")
        doc = {"schema_version": 1, "id": item_id, "account_uid": account, "oid": str(oid), "root": str(root),
               "page": page, "crawl_id": crawl_id, "resource_type": resource_type, **data,
               "source": {"type": "interaction", "recorded_at": engine.clock().isoformat(),
                          "reference": f"comment:{resource_type}:{oid}:{root}:page:{page}"}, "locked": False}
        saved = store.write("comment_pages", doc, old["version"] if old else None, engine.identity(), item_id)
        store.set_state("comment-page-written:" + item_id, saved["version"])
        return saved


def pack_pages(pages, char_limit):
    if not pages:
        return {"comments": [], "complete": False, "next_page": 1, "reason": "unavailable", "record_refs": []}
    totals = {p["data"]["total"] for p in pages}
    total = next(iter(totals)) if len(totals) == 1 else None
    page_size = pages[0]["data"]["page_size"]
    page_numbers = {p["data"]["page"] for p in pages}
    comments = {}
    root = next((p["data"]["root_comment"] for p in pages if p["data"]["root_comment"]), None)
    if root:
        comments[root["rpid"]] = root
    for page in sorted(pages, key=lambda r: r["data"]["page"]):
        comments.update((r["rpid"], r) for r in page["data"]["replies"])
    last = max(1, math.ceil(total / page_size)) if total is not None else None
    missing = next((n for n in range(1, last + 1) if n not in page_numbers), None) if last else max(page_numbers) + 1
    chosen, size, truncated = [], 0, any(c.get("text_truncated") for c in comments.values())
    for comment in comments.values():
        length = len(json.dumps(comment, ensure_ascii=False))
        if size + length <= char_limit:
            chosen.append(comment)
            size += length
        else:
            truncated = True
    complete = bool(root is not None and last is not None and missing is None
                    and len(comments) - 1 >= total and not truncated
                    and not any(p.get("manual_override") for p in pages))
    return {
        "comments": chosen, "root_rpid": root["rpid"] if root else "", "total_replies": total,
        "pages": sorted(page_numbers), "complete": complete, "context_truncated": truncated,
        "manual_override": any(p.get("manual_override") for p in pages),
        "next_page": missing, "context_chars": size,
        "record_refs": [{"id": p["data"]["id"], "version": p["version"]} for p in pages],
        "note": "平台返回的当前可见评论；分页期间可能有新增、删除或折叠，不保证覆盖不可见评论。",
    }


async def event_context(engine, event_id, page=1):
    run = engine.role_run()
    store = engine.store
    with store.db() as db:
        row = db.execute("SELECT body FROM events WHERE account=? AND id=?", (run["account"], event_id)).fetchone()
    if not row:
        raise AssistantError("unknown_event", "只能读取本账号实际收到事件的材料")
    event = engine.store.ledger_decode("events.body", row["body"])
    settings = store.read("companion")["data"]
    pages = []
    if event["kind"] == "reply" and hasattr(engine.adapter, "comment_page"):
        oid, root = int(event.get("oid", 0)), int(event.get("root", 0))
        resource_type = event.get("resource_type", 1)
        if type(resource_type) is not int or resource_type not in (1, 11, 17):
            raise AssistantError("comment_resource", "评论资源类型不受支持")
        if oid and root:
            resource_key = "" if resource_type == 1 else f"{resource_type}:"
            crawl_key = f"comment-crawl:{run['account']}:{resource_key}{oid}:{root}"
            crawl = store.state(crawl_key)
            if page == 1 or not crawl:
                crawl = {"id": uuid.uuid4().hex, "started_at": engine.clock().isoformat()}
                store.set_state(crawl_key, crawl)
            limit = settings.get("comment_context_pages", 3)
            pending = list(range(page, min(10001, page + limit)))
            visited = set()
            while pending and len(visited) < limit:
                number = pending.pop(0)
                if number in visited:
                    continue
                visited.add(number)
                try:
                    item_id = comment_page_id(run["account"], oid, root, number, resource_type)
                    try:
                        before = store.read("comment_pages", item_id)["version"]
                    except AssistantError as exc:
                        if exc.code != "not_found":
                            raise
                        before = None
                    if resource_type == 1:
                        data = await engine.adapter.comment_page(oid, root, number)
                    else:
                        data = await engine.adapter.comment_page(oid, root, number, resource_type=resource_type)
                    engine.run(run["generation"])
                    pages.append(save_page(engine, oid, root, number, data, crawl["id"], before, resource_type=resource_type))
                    total = data["total"]
                    if total is not None:
                        last = max(1, math.ceil(total / data["page_size"]))
                        pending = [n for n in pending if n <= last]
                        # New notifications usually refer to recent replies. Reserve a page for
                        # the tail when the triggering comment is absent from the opening page.
                        if page == 1 and number == 1 and 1 < last <= 10000 and not any(
                            r["rpid"] == str(event.get("parent", "")) for r in data["replies"]
                        ):
                            pending = [last, *pending]
                except AssistantError as exc:
                    if exc.code not in ("platform_read_error", "read_timeout", "comment_root_mismatch"):
                        raise
                    event["comment_context_error"] = exc.code
                    break
            # Include previously fetched pages, retaining editable files as the source of truth.
            cached = [r for r in store.records("comment_pages")[0]
                      if r["data"]["account_uid"] == run["account"] and r["data"]["oid"] == str(oid)
                       and r["data"]["root"] == str(root) and r["data"].get("resource_type", 1) == resource_type]
            seen = {p["data"]["id"] for p in pages}
            pages += [r for r in cached if r["data"]["crawl_id"] == crawl["id"] and r["data"]["id"] not in seen]
            for record in pages:
                if record["version"] != store.state("comment-page-written:" + record["data"]["id"]):
                    record["manual_override"] = True
            event["comment_context"] = pack_pages(pages, settings.get("comment_context_chars", 12000))
            event["comment_context"]["cached_pages"] = sorted({r["data"]["page"] for r in cached})
            event["comment_context"]["snapshot_started_at"] = crawl["started_at"]
    attachments = list(event.get("attachments", []))
    for comment in event.get("comment_context", {}).get("comments", []):
        attachments += [{**a, "comment_rpid": comment["rpid"]} for a in comment["attachments"]]
    event["images"] = []
    event["images_available"] = sum(a["kind"] == "image" for a in attachments)
    if settings["visuals_enabled"]:
        for attachment in attachments:
            if attachment["kind"] != "image" or len(event["images"]) >= settings["visuals_max_images"]:
                continue
            try:
                path = await download_image(store, attachment["url"])
                engine.run(run["generation"])
                event["images"].append({
                    "kind": "message_image", "path": path, "event_id": event_id,
                    "comment_rpid": attachment.get("comment_rpid", ""),
                })
            except (httpx.HTTPError, OSError, ValueError) as exc:
                event["image_error"] = type(exc).__name__
            except AssistantError as exc:
                if exc.code not in ("image_url", "image_format", "image_size", "invalid_image"):
                    raise
                event["image_error"] = exc.code
    event["images_omitted"] = event["images_available"] - len(event["images"])
    from .persona import participant

    event["relationship_context"] = participant(event, store.read("persona")["data"])
    return event
