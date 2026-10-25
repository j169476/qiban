"""Small adapter against pinned bilibili-api-python; no automatic POST retries."""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from . import credentials
from .errors import AssistantError
from .mentions import validate_mentions


class Bilibili:
    def __init__(self, store):
        self.store = store

    async def _read(self, operation):
        for attempt in range(2):
            try:
                return await asyncio.wait_for(operation(), timeout=20)
            except (TimeoutError, httpx.TransportError):
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise AssistantError("read_timeout", "B站读取超时") from None
            except AssistantError:
                raise
            except Exception as exc:
                # Never expose the exception repr: upstream can include cookies or HTTP parameters.
                code = getattr(exc, "code", None)
                status = getattr(exc, "status", None)
                if code == -101:
                    raise AssistantError("login_required", "B站登录已失效，请重新登录") from None
                detail = type(exc).__name__
                if isinstance(code, int):
                    detail += f" code={code}"
                if isinstance(status, int):
                    detail += f" HTTP={status}"
                raise AssistantError("platform_read_error", f"B站读取失败 ({detail})") from None

    async def me(self):
        from bilibili_api import user

        credential = credentials.load(self.store)
        data = await self._read(lambda: user.get_self_info(credential))
        return {"uid": str(data["mid"]), "name": data["name"]}

    async def maintain_credentials(self, account):
        return await credentials.maintain(self.store, account)

    async def video_interaction_status(self, bvid):
        from bilibili_api import video

        item = video.Video(bvid=bvid, credential=credentials.load(self.store))
        liked = await self._read(item.has_liked)
        coins = await self._read(item.get_pay_coins)
        if type(liked) not in (bool, int) or liked not in (0, 1) or type(coins) is not int or coins < 0:
            raise AssistantError("platform_read_error", "视频互动状态响应不完整，未猜测是否成功")
        return {"bvid": bvid, "liked": bool(liked), "coins": coins}

    async def _video_post(self, kind, item, credential, data, before_send):
        from bilibili_api.utils.network import HEADERS, Api
        from bilibili_api.utils.utils import get_api

        api = Api(**get_api("video")["operate"][kind], credential=credential)
        api.update_data(aid=item.get_aid(), bvid=item.get_bvid(), **data)
        # Keep the actual target as request context, with the SDK's normal browser headers.
        api.update_headers(**{
            **HEADERS, "Origin": "https://www.bilibili.com",
            "Referer": f"https://www.bilibili.com/video/{item.get_bvid()}/",
        })
        # Reuse the SDK's cookie/CSRF preparation and transport, but guard *after*
        # any preparatory network work and send exactly once (including WBI errors).
        from .publication import post_once

        return await post_once(api, before_send)

    async def search(self, query, limit=10):
        from bilibili_api import search

        if not query or len(query) > 100:
            raise AssistantError("invalid_query", "搜索词长度为 1 至 100")
        data = await self._read(
            lambda: search.search_by_type(
                query, search_type=search.SearchObjectType.VIDEO, page_size=min(limit, 20)
            )
        )
        return [self._candidate(row) for row in (data.get("result") or [])[: min(limit, 20)]]

    @staticmethod
    def _candidate(row):
        bvid = row.get("bvid", "")
        return {
            "bvid": bvid,
            "title": html.unescape(re.sub("<[^>]+>", "", row.get("title", ""))),
            "description": row.get("description", row.get("desc", ""))[:2000],
            "owner_uid": str(row.get("mid", row.get("owner", {}).get("mid", ""))),
            "url": f"https://www.bilibili.com/video/{bvid}",
            "coverage": "metadata_only",
        }

    async def feed(self, following=True, limit=10):
        from bilibili_api import dynamic, homepage

        credential = credentials.load(self.store, optional=not following)
        if following:
            data = await self._read(lambda: dynamic.get_dynamic_page_info(credential))
            rows = []
            for item in data.get("items", []):
                modules = item.get("modules", {})
                archive = (modules.get("module_dynamic", {}).get("major") or {}).get("archive")
                if archive:
                    archive["mid"] = modules.get("module_author", {}).get("mid", "")
                    rows.append(archive)
        else:
            data = await self._read(lambda: homepage.get_videos(credential))
            rows = data.get("item", [])
        return [self._candidate(row) for row in rows[: min(limit, 20)] if row.get("bvid")]

    async def video_context(self, bvid, page_index=0, detail=False, start_seconds=0, end_seconds=None):
        from bilibili_api import video

        if not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
            raise AssistantError("invalid_bvid", "BV 号格式错误")
        v = video.Video(bvid=bvid, credential=credentials.load(self.store, optional=True))
        info = await self._read(v.get_info)
        pages = info.get("pages", [])
        if not 0 <= page_index < len(pages):
            raise AssistantError("invalid_page", "分 P 序号超出范围")
        page = pages[page_index]
        duration = page.get("duration", info.get("duration", 0))
        coverage = {
            "kind": "metadata_only",
            "bvid": bvid,
            "start_seconds": 0,
            "end_seconds": 0,
            "duration_seconds": duration,
            "complete": False,
        }
        result = {
            "bvid": bvid,
            "aid": info["aid"],
            "title": info["title"],
            "description": info.get("desc", ""),
            "owner_uid": str(info["owner"]["mid"]),
            "page_index": page_index,
            "cid": str(page["cid"]),
            "url": f"https://www.bilibili.com/video/{bvid}?p={page_index + 1}",
            "coverage": coverage,
            "segments": [],
            "material_status": "metadata_only",
        }
        subtitle_limit = self.store.read("companion")["data"]["subtitle_char_limit"]
        try:
            sub = await self._read(lambda: v.get_subtitle(cid=page["cid"]))
            tracks = (sub or {}).get("subtitles", [])
            if not tracks:
                return result
            track = sorted(tracks, key=lambda t: not str(t.get("lan", "")).startswith("zh"))[0]
            url = track["subtitle_url"]
            if url.startswith("//"):
                url = "https:" + url
            parsed = urlsplit(url)
            # Fetch only the subtitle CDN URL returned by Bilibili; never follow arbitrary redirects.
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or not any(
                    parsed.hostname.endswith("." + suffix) or parsed.hostname == suffix
                    for suffix in ("hdslb.com", "bilibili.com")
                )
            ):
                raise AssistantError("subtitle_url", "不接受非 B站字幕域名")
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 2_000_000:
                            raise AssistantError("subtitle_size", "字幕超出本轮读取上限")
            body = json.loads(raw)["body"]
            window_end = min(float(end_seconds), duration) if end_seconds is not None else duration
            body = sorted(body, key=lambda s: float(s["from"]))
            selected = [s for s in body if float(s["to"]) > start_seconds and float(s["from"]) < window_end]
            segments = []
            chars = 0
            for segment in selected:
                text = str(segment["content"])
                if chars + len(text) > subtitle_limit:
                    break
                segments.append({"start": float(segment["from"]), "end": float(segment["to"]), "text": text})
                chars += len(text)
            if segments:
                coverage.update(
                    kind="subtitles",
                    start_seconds=segments[0]["start"],
                    end_seconds=segments[-1]["end"],
                    complete=start_seconds == 0 and window_end >= duration and len(segments) == len(body),
                )
                result.update(
                    segments=segments,
                    material_status="subtitles",
                    language=track.get("lan"),
                    coverage_note="覆盖字幕轨；未验证画面或无字幕语音",
                    subtitle_window={"start_seconds": start_seconds,
                                     "end_seconds": window_end if len(segments) == len(selected) else segments[-1]["end"],
                                     "truncated": len(segments) != len(selected)},
                )
        except Exception:
            result["material_status"] = "subtitle_unavailable"
        finally:
            from .media import attach_visuals

            await attach_visuals(self, v, info, page, result)
            from .video_material import enhance

            await enhance(self, v, page, result, detail=detail, start_seconds=start_seconds, end_seconds=end_seconds)
        return result

    @staticmethod
    def _notification_events(rows, time_key, since):
        from bilibili_api.utils.aid_bvid_transformer import aid2bvid

        from .rich_content import comment_attachments

        events = []
        # Numeric resource types come from the platform notification, never from
        # model inference or a URL guessed from its text. Unknown variants stay read-only.
        seen = set()
        for row in rows:
            item = row.get("item", {})
            ts = row.get(time_key, 0)
            resource_type = item.get("business_id")
            if (
                ts < since
                or type(resource_type) is not int or resource_type not in (1, 11, 17)
                or item.get("type") not in ({"reply"} if resource_type == 1 else {"reply", "dynamic"})
                or not item.get("subject_id")
                or not item.get("source_id")
            ):
                continue
            # A reply that also @mentions us can occur in both notification feeds.
            resource_key = "" if resource_type == 1 else f"{resource_type}-"
            event_id = f"reply-{resource_key}{item['subject_id']}-{item['source_id']}"
            if event_id in seen:
                continue
            seen.add(event_id)
            users = row.get("user", {})
            events.append(
                {
                    "id": event_id,
                    "kind": "reply",
                    "uid": str(users["mid"]),
                    "display_name": str(users.get("nickname", users.get("uname", users.get("name", ""))))[:120],
                    "bvid": aid2bvid(int(item["subject_id"])) if resource_type == 1 else "",
                    "resource_type": resource_type,
                    "received": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                    "text": item.get("source_content", "")[:16000],
                    "attachments": comment_attachments(item),
                    "oid": int(item["subject_id"]),
                    "root": int(item.get("root_id") or item["source_id"]),
                    "parent": int(item["source_id"]),
                }
            )
        return events

    def _page_resume(self, key, account, started_at):
        state = self.store.state(key) or {}
        pending = state.get("pending") if state.get("started_at") == started_at else None
        if pending:
            # Engine intentionally excludes our own events from its inbox table.
            # Old pending pages may still contain them; they cannot await an ACK.
            events = [event for event in pending["events"] if event["uid"] != account]
            with self.store.db() as db:
                persisted = all(db.execute("SELECT 1 FROM events WHERE account=? AND id=?", (
                    account, event["id"])).fetchone() for event in events)
            if not persisted:
                # A pause/crash or a failed sibling feed can happen after this page
                # was fetched but before Engine.inbox commits the combined batch.
                return events, None
            return None, pending["next_cursor"]
        return None, None

    async def _notification_page(self, fetch, credential, source, time_key, account, started_at, since):
        """One durable page per feed; advance only after its events reach the inbox DB."""
        key = f"inbox-notification-page:{account}:{source}"
        replay, cursor = self._page_resume(key, account, started_at)
        if replay is not None:
            return replay
        params = {}
        if cursor:
            params = {"last_reply_id" if source == "replies" else "last_id": cursor["id"],
                      time_key: cursor["time"]}
        data = await self._read(lambda: fetch(credential, **params))
        events = [event for event in self._notification_events(data.get("items", []), time_key, since)
                  if event["uid"] != account]
        value = data.get("cursor") or {}
        next_cursor = None
        if (value.get("is_end") in (False, 0) and type(value.get("id")) is int and value["id"] > 0
                and type(value.get("time")) is int and value["time"] >= since):
            candidate = {"id": value["id"], "time": value["time"]}
            # Refuse repeated/forward cursors so malformed responses cannot strand
            # the feed. End of history resets to the head for fresh notifications.
            if not cursor or (candidate["time"], candidate["id"]) < (cursor["time"], cursor["id"]):
                next_cursor = candidate
        self.store.set_state(key, {"started_at": started_at, "pending": {
            "events": events, "next_cursor": next_cursor,
        }})
        return events

    def _pending_private_uids(self, account, started_at):
        """Keep discovered backlog in the fair queue when it drops out of the server head."""
        prefix = f"inbox-private-page:{account}:"
        result = set()
        with self.store.db() as db:
            for row in db.execute("SELECT key,value FROM state WHERE key LIKE ?", (prefix + "%",)):
                uid = row["key"][len(prefix):]
                state = self.store.ledger_decode("state:" + row["key"], row["value"])
                if not uid.isdigit() or state.get("started_at") != started_at:
                    continue
                pending = state.get("pending") or {}
                if pending.get("next_cursor") is not None or any(not db.execute(
                        "SELECT 1 FROM events WHERE account=? AND id=?", (account, event["id"])).fetchone()
                        for event in pending.get("events", []) if event["uid"] != account):
                    result.add(int(uid))
        return result

    async def _session_page(self, credential, account, started_at, since):
        """Discover one bounded list page, atomically retaining its unscheduled UIDs."""
        from bilibili_api import session

        key = f"inbox-session-page:{account}"
        state = self.store.state(key) or {}
        if state.get("started_at") != started_at:
            state = {"started_at": started_at, "next_cursor": None, "uids": []}
        # Backpressure leaves the cursor untouched: drain discovered work before
        # fetching another page instead of discarding its overflow. A real list
        # page has at most 20 entries, keeping this initial-read queue under 60.
        queued = {uid for uid in state["uids"] if type(uid) is int and uid > 0 and str(uid) != account}
        if len(queued) >= 40:
            return key, state, queued
        cursor = state.get("next_cursor")
        if cursor:
            from bilibili_api.utils.network import Api
            from bilibili_api.utils.utils import get_api

            # get_sessions exposes no pagination arguments. Preserve its request
            # contract and default page size while adding the history upper bound.
            api = Api(**get_api("session")["session"]["get"], credential=credential)
            api.update_params(session_type=1, group_fold=1, unfollow_fold=0, sort_rule=2,
                              build=0, mobi_app="web", size=20, end_ts=cursor)
            data = await self._read(lambda: api.result)
        else:
            data = await self._read(lambda: session.get_sessions(credential, session_type=1))
        rows = data.get("session_list") or []
        times = [row["session_ts"] for row in rows
                 if type(row.get("session_ts")) is int and row["session_ts"] > 0]
        for row in rows:
            uid, timestamp = row.get("talker_id"), row.get("session_ts")
            if (type(uid) is int and uid > 0 and str(uid) != account and row.get("session_type", 1) == 1
                    and type(timestamp) is int and timestamp / 1_000_000 >= since):
                queued.add(uid)
        next_cursor = None
        if times and data.get("has_more") in (True, 1) and min(times) / 1_000_000 >= since:
            candidate = min(times)
            if not cursor or candidate < cursor:
                next_cursor = candidate
        state = {"started_at": started_at, "next_cursor": next_cursor, "uids": sorted(queued)}
        # Advancing the list cursor is safe only together with persisting every
        # discovered UID. Private-page persistence takes over when it is scheduled.
        self.store.set_state(key, state)
        return key, state, queued

    async def _private_page(self, uid, credential, account, started_at, since):
        from bilibili_api import session

        from .rich_content import private_content

        key = f"inbox-private-page:{account}:{uid}"
        replay, cursor = self._page_resume(key, account, started_at)
        if replay is not None:
            return replay
        if cursor:
            from bilibili_api.utils.network import Api
            from bilibili_api.utils.utils import get_api

            # SDK fetch_session_msgs exposes only begin_seqno, the lower bound
            # used by its new-message poller. Older pages need an exclusive upper
            # bound instead, using the same read API without raising its size.
            api = Api(**get_api("session")["session"]["fetch"], credential=credential)
            api.update_params(talker_id=uid, session_type=1, size=30, begin_seqno=0, end_seqno=cursor)
            history = await self._read(lambda: api.result)
        else:
            history = await self._read(lambda: session.fetch_session_msgs(uid, credential))
        messages = history.get("messages") or []
        events = []
        for msg in messages:
            ts = msg.get("timestamp", 0)
            if ts < since or int(msg.get("sender_uid", 0)) != uid or str(uid) == account:
                continue
            content = private_content(msg)
            if content is None:
                continue
            events.append({"id": f"dm-{uid}-{msg['msg_seqno']}", "kind": "dm_reply", "uid": str(uid),
                           "received": datetime.fromtimestamp(ts, timezone.utc).isoformat(), **content})
        sequences = [msg["msg_seqno"] for msg in messages
                     if type(msg.get("msg_seqno")) is int and msg["msg_seqno"] > 0]
        next_cursor = None
        if (sequences and history.get("has_more", len(messages) >= 30) in (True, 1)
                and not any(msg.get("timestamp", 0) < since for msg in messages)):
            candidate = min(sequences)
            if not cursor or candidate < cursor:
                next_cursor = candidate
        self.store.set_state(key, {"started_at": started_at, "pending": {
            "events": events, "next_cursor": next_cursor,
        }})
        return events

    async def inbox(self, started_at):
        from bilibili_api import session

        credential = credentials.load(self.store)
        account = self.store.read("manifest")["data"]["account_uid"]
        since = datetime.fromisoformat(started_at).timestamp()
        replies = await self._notification_page(session.get_replies, credential, "replies", "reply_time",
                                                account, started_at, since)
        mentions = await self._notification_page(session.get_at, credential, "mentions", "at_time",
                                                 account, started_at, since)
        # Both feeds can describe the same comment; persistence also deduplicates
        # across polls, without truncating the tail before it is committed.
        combined = {}
        for event in [*replies, *mentions]:
            combined.setdefault(event["id"], event)
        events = list(combined.values())
        list_key, list_state, conversations = await self._session_page(credential, account, started_at, since)
        # Bound HTTP work, not the returned event batch. The caller persists and
        # deduplicates the whole batch before choosing work for the model. A
        # stable UID cursor lets quiet conversations behind the first ten make
        # progress even when the server keeps returning the same ordered list.
        cursor_key = f"inbox-session-cursor:{account}"
        state = self.store.state(cursor_key) or {}
        last_uid = state.get("uid", 0) if state.get("started_at") == started_at else 0
        conversations |= self._pending_private_uids(account, started_at)
        ordered = sorted(conversations)
        selected = ([uid for uid in ordered if uid > last_uid]
                    + [uid for uid in ordered if uid <= last_uid])[:10]
        for uid in selected:
            events.extend(await self._private_page(uid, credential, account, started_at, since))
        if selected:
            # This is a circular fairness cursor, not a delivery acknowledgement.
            # A crash before caller persistence merely delays this batch until
            # the next sweep; it never declares any event consumed.
            with self.store.db() as db:
                self.store.set_state(cursor_key, {"started_at": started_at, "uid": selected[-1]}, db)
                self.store.set_state(list_key, {**list_state, "uids": [
                    uid for uid in list_state["uids"] if uid not in selected]}, db)
        return events

    async def comment_page(self, oid, root, page=1, *, resource_type=1):
        from bilibili_api import comment

        from .rich_content import normalize_comment

        if type(resource_type) is not int or resource_type not in (1, 11, 17):
            raise AssistantError("comment_resource", "评论资源类型不受支持")
        credential = credentials.load(self.store)
        data = await self._read(lambda: comment.Comment(
            oid, comment.CommentResourceType(resource_type), root, credential=credential,
        ).get_sub_comments(page_index=page, page_size=20))
        metadata = data.get("page") or {}
        total = metadata.get("count")
        total = total if type(total) is int and total >= 0 else None
        replies = [r for row in (data.get("replies") or [])[:20] if (r := normalize_comment(row))]
        root_comment = normalize_comment(data.get("root"))
        if root_comment and root_comment["rpid"] != str(root):
            raise AssistantError("comment_root_mismatch", "评论楼接口返回了不同的根评论")
        page_size = metadata.get("size", 20)
        return {
            "total": total, "page_size": min(20, max(1, page_size)) if type(page_size) is int else 20,
            "root_comment": root_comment, "replies": replies,
        }

    async def perform(self, kind, payload, policy, before_send):
        from bilibili_api import dynamic, session, user, video
        from bilibili_api.exceptions import ResponseCodeException
        from bilibili_api.utils.network import Api
        from bilibili_api.utils.utils import get_api

        from .publication import post_once

        credential = credentials.load(self.store)
        v = video.Video(bvid=payload["bvid"], credential=credential) if "bvid" in payload else None

        async def send_comment(oid, root=None, parent=None, resource_type=1):
            if type(resource_type) is not int or resource_type not in (1, 11, 17):
                raise AssistantError("platform_rejected", "评论资源类型不受支持")
            mentions = validate_mentions(payload["text"], payload.get("mentions", {}))
            # The pinned library's send_comment has no mention-map argument.
            # Prepare the same request for both forms, then guard immediately
            # before single dispatch instead of entering the SDK retry loop.
            data = {
                "oid": oid,
                "type": resource_type,
                "message": payload["text"],
                "plat": 1,
                "statistics": {"appId": 100, "platform": 5},
                "gaia_source": "main_web",
            }
            if mentions:
                data["at_name_to_mid"] = json.dumps(
                    {name: int(uid) for name, uid in mentions.items()}, ensure_ascii=False)
            if root is not None:
                data.update(root=root, parent=parent or root)
            api = Api(**get_api("common")["comment"]["send"], credential=credential).update_data(**data)
            return await post_once(api, before_send)

        async def dispatch():
            if kind == "comment":
                return await send_comment(v.get_aid())
            if kind == "reply":
                event = payload["_event"]
                return await send_comment(event["oid"], root=event["root"], parent=event["parent"],
                                          resource_type=event.get("resource_type", 1))
            if kind == "dm_reply":
                sender = (int(credential.dedeuserid) if credential.has_dedeuserid()
                          and int(credential.dedeuserid) else int((await user.get_self_info(credential))["mid"]))
                receiver = int(payload["_event"]["uid"])
                api = Api(**get_api("session")["operate"]["send_msg"], credential=credential)
                api.update_params(w_sender_uid=sender, w_receiver_id=receiver)
                api.update_data(**{
                    "msg[sender_uid]": sender, "msg[receiver_id]": receiver,
                    "msg[receiver_type]": 1, "msg[msg_type]": session.EventType.TEXT.value,
                    "msg[msg_status]": 0, "msg[content]": json.dumps({"content": payload["text"]}),
                    "msg[dev_id]": "A6716E9A-7CE3-47AF-994B-F0B34178D28D",
                    "msg[new_face_version]": 0, "msg[timestamp]": int(time.time()),
                    "from_filework": 0, "build": 0, "mobi_app": "web",
                })
                return await post_once(api, before_send)
            if kind == "comment_like":
                event = payload["_event"]
                resource_type = event.get("resource_type", 1)
                if type(resource_type) is not int or resource_type not in (1, 11, 17):
                    raise AssistantError("platform_rejected", "评论资源类型不受支持")
                api = Api(**get_api("common")["comment"]["like"], credential=credential)
                api.update_data(oid=event["oid"], type=resource_type,
                                rpid=event["parent"], action=1)
                await post_once(api, before_send)
                return {"rpid": str(event["parent"])}
            if kind == "dynamic":
                mentions = validate_mentions(payload["text"], payload.get("mentions", {}))
                content = dynamic.BuildDynamic.empty()
                offset = 0
                if mentions:
                    pattern = r"@(" + "|".join(re.escape(name) for name in mentions) + r")(?=\s|$)"
                    for match in re.finditer(pattern, payload["text"]):
                        content.add_plain_text(payload["text"][offset : match.start()])
                        content.add_at(uid=int(mentions[match[1]]), uname=match[1])
                        offset = match.end()
                content.add_plain_text(payload["text"][offset:])
                if payload.get("images"):
                    from .publication import send_images

                    return await send_images(self.store, content, payload["images"], credential, before_send)
                contents = await content.get_contents(credential=credential)
                api = Api(**get_api("dynamic")["send"]["instant"], credential=credential, json_body=True)
                api.update_data(dyn_req={
                    "content": {"contents": contents}, "scene": content.get_dynamic_type().value,
                    "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
                    "attach_card": None,
                })
                api.update_params(csrf=credential.bili_jct)
                return await post_once(api, before_send)
            if kind == "like":
                liked = await v.has_liked()
                if type(liked) not in (bool, int) or liked not in (0, 1):
                    raise AssistantError("action_unconfirmed", "无法核实既有点赞状态，未发起新点赞")
                if liked:
                    return {"already_done": True}
                await self._video_post("like", v, credential, {"like": 1}, before_send)
                try:
                    liked = await v.has_liked()
                    if type(liked) not in (bool, int) or liked != 1:
                        raise ValueError()
                except Exception:
                    raise AssistantError(
                        "action_unconfirmed", "点赞接口返回成功，但读回状态未确认；不自动重发"
                    ) from None
                return {"verified": True}
            if kind == "favorite":
                if not policy["favorite_folder_id"]:
                    raise AssistantError("platform_rejected", "未配置收藏夹 ID")
                # has_favoured refers to any folder; conservative no duplicate additions.
                if await v.has_favoured():
                    return {"already_done": True}
                api = Api(**get_api("video")["operate"]["favorite"], credential=credential)
                api.update_data(rid=v.get_aid(), type=2, add_media_ids=str(policy["favorite_folder_id"]),
                                del_media_ids="")
                return await post_once(api, before_send)
            if kind == "coin":
                paid = await v.get_pay_coins()
                if type(paid) is not int or not 0 <= paid <= 2:
                    raise AssistantError("action_unconfirmed", "无法核实既有投币数量，未发起新投币")
                if paid >= payload["units"]:
                    return {"already_done": True}
                remaining = payload["units"] - paid
                result = await self._video_post(
                    "coin", v, credential, {"multiply": remaining, "select_like": 0}, before_send
                )
                try:
                    observed = await v.get_pay_coins()
                    if type(observed) is not int or observed < payload["units"]:
                        raise ValueError()
                except Exception:
                    raise AssistantError(
                        "action_unconfirmed", "投币接口返回成功，但读回数量未确认；不自动重发"
                    ) from None
                return {**(result or {}), "units_spent": remaining, "verified": True}
            if kind == "follow":
                target = user.User(int(payload["uid"]), credential=credential)
                relation = await target.get_relation()
                attribute = (relation.get("be_relation") or {}).get("attribute")
                if attribute in (1, 2, 6):
                    return {"already_done": True}
                if attribute != 0:
                    raise AssistantError("platform_rejected", "无法确认可关注状态")
                api = Api(**get_api("user")["operate"]["modify"], credential=credential)
                api.update_data(fid=int(payload["uid"]), act=user.RelationType.SUBSCRIBE.value, re_src=11)
                return await post_once(api, before_send)
            raise AssistantError("platform_rejected", "动作未实现")

        try:
            data = await asyncio.wait_for(dispatch(), timeout=20)
        except ResponseCodeException as exc:
            code = getattr(exc, "code", None)
            suffix = f" (code={code})" if isinstance(code, int) else ""
            raise AssistantError("platform_rejected", "B站明确拒绝此次操作" + suffix) from None
        # Keep only non-secret stable identifiers.
        allowed = ("rpid", "dynamic_id", "dynamic_id_str", "msg_key", "already_done", "units_spent", "verified")
        result = {k: data[k] for k in allowed if isinstance(data, dict) and k in data}
        if kind == "dynamic" and isinstance(data, dict):
            dynamic_id = data.get("dyn_id_str") or data.get("dyn_id")
            if dynamic_id is not None:
                result["dynamic_id_str"] = str(dynamic_id)
        if kind == "dynamic" and payload.get("images") and not (
            result.get("dynamic_id_str") or result.get("dynamic_id")
        ):
            raise AssistantError("action_unconfirmed", "图文动态响应未提供可核对 ID；不自动重发")
        if result.get("already_done"):
            result["units_spent"] = 0
        return result
