"""Bounded favorite-folder tools over the pinned SDK.

Writes are transport primitives: the caller must reserve a durable action and
provide its last-moment dispatch guard. No model or scheduler runs in this module.
Folder attribute bits are documented by the API's reverse-engineering project:
https://github.com/bilibili-plugins/bilibili-api-collect/blob/master/docs/fav/info.md
"""
import asyncio
import hashlib
import json
import re

from .errors import AssistantError

PAGE_SIZE = 20
BV = re.compile(r"BV[A-Za-z0-9]{10}")


def invalid(message="收藏夹响应不完整，请重新读取。"):
    raise AssistantError("favorite_state", message)


def folder(raw, account):
    if (not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] <= 0
            or str(raw.get("mid")) != account or type(raw.get("attr")) is not int or raw["attr"] < 0
            or not isinstance(raw.get("title"), str)
            or type(raw.get("media_count")) is not int or raw["media_count"] < 0):
        invalid()
    return {"id": raw["id"], "title": raw["title"][:80], "private": bool(raw["attr"] & 1),
            "default": not bool(raw["attr"] & 2), "count": raw["media_count"]}


def catalog(raw, account):
    if not isinstance(raw, dict) or type(raw.get("count")) is not int or raw["count"] < 0:
        invalid()
    rows = raw.get("list")
    if rows is None and raw["count"] == 0:
        rows = []
    if not isinstance(rows, list) or raw["count"] != len(rows):
        invalid("收藏夹目录不完整，不能据此选定或修改收藏夹。")
    result = [folder(row, account) for row in rows]
    if len({item["id"] for item in result}) != len(result):
        invalid()
    return sorted(result, key=lambda item: item["id"])


def revision(items):
    return hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def selected(items, folder_id):
    if type(folder_id) is not int or folder_id <= 0:
        invalid("需要有效的收藏夹 ID。")
    match = next((item for item in items if item["id"] == folder_id), None)
    if match is None:
        invalid("收藏夹不存在或不属于当前账号，请重新读取。")
    return match


class FavoriteTools:
    def __init__(self, adapter, account, guard):
        self.adapter, self.account, self.guard = adapter, str(account), guard
        guard()
        provider = adapter.credential_provider
        self.login_revision = provider.revision() if provider is not None else None
        self.credential = adapter._credential()
        if str(getattr(self.credential, "dedeuserid", "")) != self.account:
            raise AssistantError("account_mismatch", "收藏夹凭据与绑定账号不符。")

    def current(self):
        self.guard()
        provider = self.adapter.credential_provider
        if provider is not None:
            provider._current(self.login_revision)

    async def _read(self, operation):
        self.current()
        result = await self.adapter._read(operation)
        self.current()
        return result

    async def _catalog(self, bvid=None):
        from bilibili_api import favorite_list, video

        item = video.Video(bvid=bvid, credential=self.credential) if bvid else None
        raw = await self._read(lambda: favorite_list.get_video_favorite_list(
            int(self.account), video=item, credential=self.credential))
        rows = catalog(raw, self.account)
        if bvid:
            states = {}
            for entry in raw["list"]:
                if type(entry.get("fav_state")) is not int or entry["fav_state"] not in (0, 1):
                    invalid("无法核对视频当前所在的收藏夹。")
                states[entry["id"]] = bool(entry["fav_state"])
            return rows, states
        return rows

    async def list(self, *, offset=0, expected=None):
        if type(offset) is not int or offset < 0:
            invalid("收藏夹分页位置无效。")
        items = await self._catalog()
        stamp = revision(items)
        if expected is not None and stamp != expected:
            invalid("收藏夹目录已经改变，请从第一页重新读取。")
        end = offset + PAGE_SIZE
        return {"folders": items[offset:end], "total": len(items), "revision": stamp,
                "next_offset": end if end < len(items) else None}

    async def default(self):
        matches = [item for item in await self._catalog() if item["default"]]
        if len(matches) != 1:
            invalid("未找到唯一的默认收藏夹，请在应用内选择收藏夹。")
        return matches[0]["id"]

    async def contents(self, folder_id, *, page=1, keyword=""):
        from bilibili_api import favorite_list

        if (type(page) is not int or page < 1 or not isinstance(keyword, str) or len(keyword) > 80):
            invalid("收藏夹页码或搜索词无效。")
        own = selected(await self._catalog(), folder_id)
        raw = await self._read(lambda: favorite_list.get_video_favorite_list_content(
            folder_id, page=page, keyword=keyword or None, credential=self.credential))
        if not isinstance(raw, dict) or raw.get("has_more") not in (True, False, 0, 1):
            invalid()
        info = raw.get("info")
        if (not isinstance(info, dict) or info.get("id") != folder_id
                or str(info.get("upper", {}).get("mid")) != self.account):
            invalid()
        media = raw.get("medias")
        if media is None and not raw["has_more"]:
            media = []
        if not isinstance(media, list) or len(media) > PAGE_SIZE:
            invalid()
        items, omitted = [], 0
        for item in media:
            if (not isinstance(item, dict) or item.get("type") != 2
                    or not isinstance(item.get("bvid"), str) or not BV.fullmatch(item["bvid"])
                    or not isinstance(item.get("title"), str)):
                omitted += 1
                continue
            items.append({"bvid": item["bvid"], "title": item["title"][:160],
                          "description": str(item.get("intro", ""))[:240],
                          "unavailable": item.get("attr") != 0})
        return {"folder": own, "items": items, "omitted": omitted,
                "next_page": page + 1 if raw["has_more"] else None}

    async def _post(self, operation, data, before_send):
        from bilibili_api.exceptions import ResponseCodeException
        from bilibili_api.utils.network import Api
        from bilibili_api.utils.utils import get_api

        from .publication import post_once

        api = Api(**get_api("favorite-list")["operate"][operation], credential=self.credential)
        api.update_data(**data)

        def check():
            try:
                self.current()
                before_send()
            except AssistantError:
                raise AssistantError("before_send_cancelled", "收藏操作提交前运行或账号已改变。") from None

        try:
            return await asyncio.wait_for(post_once(api, check), timeout=20)
        except ResponseCodeException:
            raise AssistantError("platform_rejected", "B 站拒绝了收藏夹操作。") from None
        except AssistantError:
            raise
        except Exception:
            raise AssistantError("action_unconfirmed", "收藏夹操作未获确认，不自动重发。") from None

    async def create(self, title, private, *, before_send):
        if (not isinstance(title, str) or not title.strip() or title != title.strip()
                or len(title) > 80 or type(private) is not bool):
            invalid("新收藏夹需要 1 至 80 字名称和明确的公开／私密选择。")
        existing = [item for item in await self._catalog() if item["title"] == title]
        if existing:
            if len(existing) == 1 and existing[0]["private"] == private:
                return {"folder": existing[0], "already_done": True, "units_spent": 0}
            invalid("已有同名收藏夹，请选择现有收藏夹或更换名称。")
        raw = await self._post("new", {"title": title, "intro": "", "privacy": int(private), "cover": ""}, before_send)
        if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] <= 0:
            raise AssistantError("action_unconfirmed", "新建收藏夹未返回有效 ID，不自动重建。")
        # A read failure after POST is an uncertain result, never an unsent cancellation.
        try:
            result = selected(await self._catalog(), raw["id"])
            if result["title"] != title or result["private"] != private:
                invalid()
        except Exception:
            raise AssistantError("action_unconfirmed", "未能核对新建收藏夹，不自动重建。") from None
        return {"folder": result, "verified": True}

    async def move(self, source, target, bvids, *, before_send):
        from bilibili_api import video

        if (source == target or not isinstance(bvids, list) or not 1 <= len(bvids) <= PAGE_SIZE
                or any(not isinstance(item, str) or not BV.fullmatch(item) for item in bvids)
                or len(set(bvids)) != len(bvids)):
            invalid("批量移动需要不同的源／目标收藏夹，以及 1 至 20 个不重复的 BV 号。")
        rows = await self._catalog()
        selected(rows, source)
        selected(rows, target)
        pending = []
        for bvid in bvids:
            rows, states = await self._catalog(bvid)
            selected(rows, source)
            selected(rows, target)
            if states[source]:
                pending.append(bvid)
            elif not states[target]:
                invalid("视频已不在源收藏夹中，请重新读取后整理。")
        if not pending:
            return {"already_done": True, "units_spent": 0, "moved": []}
        resources = ",".join(f"{video.Video(bvid=bvid).get_aid()}:2" for bvid in pending)
        await self._post("content_move", {"src_media_id": source, "tar_media_id": target,
                                         "resources": resources}, before_send)
        try:
            for bvid in pending:
                _, states = await self._catalog(bvid)
                if states.get(source) is not False or states.get(target) is not True:
                    invalid()
        except Exception:
            raise AssistantError("action_unconfirmed", "未能确认全部视频已移动，不自动重发。") from None
        return {"moved": pending, "verified": True, "units_spent": len(pending)}
