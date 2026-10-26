"""Shared controller for CLI, MCP and background cycles."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from companion_core.execution import ActionExecution
from companion_core.lifecycle import RunLifecycle
from companion_core.relationships import RelationshipUpdates

from .errors import AssistantError
from .mentions import validate_mentions
from .schema import ACTIONS, CONFIG
from .storage import Store, utcnow


def merge(current, patch):
    result = dict(current)
    for key, value in patch.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else value
        )
    return result


class Engine(RunLifecycle, ActionExecution, RelationshipUpdates):
    def __init__(self, store: Store, host, adapter=None, clock=None):
        self.store, self.host, self.adapter = store, host, adapter
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def identity(self):
        return self.host.identity()

    def start(self, duration_minutes=None, read_only=None):
        # The platform and unified controller must never both own this account.
        with self.store.lock:
            for key in ("core-run:bilibili", "core-run"):
                core = self.store.state(key) or {}
                if core.get("status") == "active" and any(
                    platform == "bilibili" for platform, _ in core.get("accounts", [])
                ):
                    raise AssistantError("core_runtime_active", "请先暂停统一核心运行，再启动 B站旧调度")
            return super().start(duration_minutes, read_only)

    def mode(self, required=None):
        tid = self.identity()
        with self.store.db() as db:
            row = db.execute("SELECT mode FROM sessions WHERE thread=?", (tid,)).fetchone()
        if not row:
            raise AssistantError("not_bound", "请先使用管理入口或角色入口")
        if required and row["mode"] != required:
            raise AssistantError("mode_denied", f"此操作仅供 {required} 聊天使用")
        self.host.require_active(tid)
        return row["mode"]

    def open(self, mode):
        if mode not in ("admin", "role"):
            raise AssistantError("invalid_mode", "模式必须为 admin 或 role")
        self.host.require_active()
        tid = self.identity()
        with self.store.lock, self.store.db() as db:
            existing = db.execute("SELECT mode FROM sessions WHERE thread=?", (tid,)).fetchone()
            if existing and existing["mode"] != mode:
                raise AssistantError("mode_conflict", "此聊天已经绑定其他模式，请在另一条聊天使用该入口")
            db.execute("INSERT INTO sessions VALUES (?,?) ON CONFLICT DO NOTHING", (tid, mode))
            if mode == "admin":
                self.store.set_state("management_thread", tid, db)
        return self.load()

    def load(self):
        mode = self.mode()
        from .health import reconcile

        reconcile(self)
        report = self.store.validate_all()
        result = {
            "mode": mode,
            "validation": report,
            "run": self.store.state("run"),
            "config": {k: self.store.read(k) for k in CONFIG},
        }
        from .persona import state as persona_state

        result["persona_state"] = persona_state(self.store, role_chat=mode == "role")
        if mode == "admin":
            result["persona_state"]["current_chat"] = "admin"
        if mode == "role":
            from .conversations import context, thread_id
            from .emotions import current
            from .interests import interest_profile

            result["growth"] = self.store.read("growth")
            baseline = result["persona_state"]["relationship_baseline"]
            result["context"] = self.store.context(uids=[baseline["owner_uid"]] if baseline else [])
            result["guided_interests"] = interest_profile(self.store)
            account = result["config"]["manifest"]["data"]["account_uid"]
            result["conversation"] = context(self.store, account, thread_id(account, "codex", self.identity()))
            result["current_emotion"] = current(self)
            from .learning import status

            result["learning"] = status(self)
        return result

    def config_get(self, kind="policy"):
        self.mode()
        if kind not in CONFIG:
            raise AssistantError("invalid_config", "该文件不属于管理配置")
        return self.store.read(kind)

    def config_patch(self, kind, patch, expected_version):
        self.mode("admin")
        if kind not in CONFIG:
            raise AssistantError("invalid_config", "该文件不属于管理配置")
        if kind == "manifest" and any(k in patch for k in ("schema_version", "project_id")):
            raise AssistantError("immutable_project", "不能通过管理聊天更改数据空间标识")
        with self.store.lock:
            old = self.store.read(kind)
            result = self.store.write(
                kind, merge(old["data"], patch), expected_version, self.identity(), allow_locked=True
            )
        return {"saved": result, "run_loaded_policy": (self.store.state("run") or {}).get("policy_version")}


    def check_supervisor(self, run):
        from .health import check_supervisor
        check_supervisor(self.store, run)

    def status(self):
        self.mode()
        from companion_core import budget

        from . import delivery
        from .health import status

        health = status(self)
        policy = self.store.read("policy")
        manifest = self.store.read("manifest")["data"]
        day = self.clock().astimezone(ZoneInfo(policy["data"]["timezone"])).date().isoformat()
        with self.store.db() as db:
            rows = db.execute(
                """SELECT kind, SUM(units) used FROM actions
                WHERE account=? AND day=? AND status IN ('dispatching','succeeded','uncertain')
                GROUP BY kind""",
                (manifest["account_uid"], day),
            ).fetchall()
            used = {r["kind"]: r["used"] for r in rows}
            recent = [
                dict(r)
                for r in db.execute(
                    "SELECT id,kind,status,created,dispatched FROM actions ORDER BY created DESC LIMIT 10"
                )
            ]
        return {
            "run": self.store.state("run"),
            "health": health,
            "policy_version": policy["version"],
            "day": day,
            "model_quota": budget.status(self.store, policy["data"], self.clock()),
            "quota": {
                a: {
                    "used": used.get(a, 0),
                    "limit": p["daily_limit"],
                    "remaining": max(0, p["daily_limit"] - used.get(a, 0)),
                }
                for a, p in policy["data"]["actions"].items()
            },
            "recent_actions": recent,
            "delivery": delivery.status(self.store, manifest["account_uid"]),
            "validation": self.store.validate_all(),
        }

    def remember(self, kind, data, expected_version=None):
        self.mode("role")
        if kind not in ("memories", "knowledge", "diaries", "growth", "skills", "interest_signals"):
            raise AssistantError("role_write_denied", "角色只能更新内容记忆、知识、日记和成长状态")
        if kind == "growth":
            return self.store.write(kind, data, expected_version, self.identity())
        if kind == "knowledge":
            coverage = data.get("coverage", {})
            if coverage.get("kind") == "web":
                from .knowledge import source_for

                source = source_for(self.store, {"data": data}, self.store.read("manifest")["data"]["account_uid"])
                observed = source["data"] if source and source["data"].get("capture") else None
            else:
                observed = self.store.state(f"context:{coverage.get('bvid', '')}")
            if not observed or observed["coverage"] != coverage:
                raise AssistantError("unverified_source", "知识的覆盖范围必须来自实际读取的视频或网页材料")
        if kind == "skills":
            if not data.get("knowledge_ids"):
                raise AssistantError("unverified_source", "技能卡需要引用已经保存的知识")
            for knowledge_id in data["knowledge_ids"]:
                self.store.read("knowledge", knowledge_id)
        if kind == "interest_signals":
            guide = self.store.read("companion")["data"]["guide_uid"]
            event_id = data.get("source", {}).get("reference")
            with self.store.db() as db:
                event = db.execute(
                    "SELECT uid,body FROM events WHERE id=? AND account=?",
                    (event_id, self.store.read("manifest")["data"]["account_uid"]),
                ).fetchone()
            from .content_refs import cached_invitation, event_video

            original = self.store.ledger_decode("events.body", event["body"]) if event else {}
            invitation = (cached_invitation(self.store, self.store.read("manifest")["data"]["account_uid"], original)
                          or event_video(original)) if event else None
            if (
                not guide or not event or event["uid"] != guide or data.get("sender_uid") != guide
                or data.get("source", {}).get("type") != "interaction"
                or data.get("bvid") != (invitation or {}).get("bvid", original.get("bvid", ""))
            ):
                raise AssistantError("unverified_interest", "兴趣引导需来自配置中大号的真实入站事件")
        item_id = data.get("id")
        if kind in ("memories", "knowledge", "skills") and expected_version is None:
            for item in self.store.records(kind)[0]:
                if item["data"]["content"].strip() == data.get("content", "").strip():
                    return {"duplicate": True, **item}
        return self.store.write(kind, data, expected_version, self.identity(), item_id)

    def relationship(self, uid, display_name, delta, event_id, impression="", facts=None):
        from companion_core.contracts import ActorRef
        from companion_core.people import bind_relationship
        result = super().relationship(uid, display_name, delta, event_id, impression, facts)
        bind_relationship(self.store, ActorRef("bilibili", uid), uid)
        return result

    def _payload(self, kind, payload):
        if kind not in ACTIONS or not isinstance(payload, dict):
            raise AssistantError("invalid_action", "不支持此动作")
        allowed = {
            "comment": {"bvid", "text"},
            "reply": {"event_id", "text"},
            "dm_reply": {"event_id", "text"},
            "comment_like": {"event_id"},
            "dynamic": {"text"},
            "like": {"bvid"},
            "favorite": {"bvid"},
            "follow": {"uid"},
            "coin": {"bvid", "units"},
        }[kind]
        optional = {"mentions"} if kind in ("comment", "reply") else set()
        if kind == "dynamic":
            optional = {"mentions", "bvid", "images"}
        if not allowed <= set(payload) or set(payload) - allowed - optional:
            raise AssistantError("invalid_payload", f"{kind} 需要字段 {', '.join(sorted(allowed))}")
        if "bvid" in payload and not re.fullmatch(r"BV[A-Za-z0-9]{10}", str(payload["bvid"])):
            raise AssistantError("invalid_bvid", "BV 号格式错误")
        if "text" in payload and (
            not isinstance(payload["text"], str) or not payload["text"].strip() or len(payload["text"]) > 1000
        ):
            raise AssistantError("invalid_text", "互动文本长度必须为 1 至 1000 字符")
        if "uid" in payload and not re.fullmatch(r"[0-9]{1,20}", str(payload["uid"])):
            raise AssistantError("invalid_uid", "UID 格式错误")
        if kind == "coin" and (type(payload["units"]) is not int or payload["units"] not in (1, 2)):
            raise AssistantError("invalid_units", "单个视频投币数为 1 或 2")
        if "event_id" in payload and (
            not isinstance(payload["event_id"], str) or len(payload["event_id"]) > 200
        ):
            raise AssistantError("invalid_event", "互动事件 ID 无效")
        if "images" in payload:
            images = payload["images"]
            if (not isinstance(images, list) or not 1 <= len(images) <= 9 or any(
                    not isinstance(r, dict) or set(r) != {"id", "version"}
                    or not isinstance(r["id"], str) or not isinstance(r["version"], str) for r in images)
                    or len({r["id"] for r in images}) != len(images)):
                raise AssistantError("invalid_images", "动态图片须为 1 至 9 个不重复的已导入 id/version 引用")
            from .publication import resolve

            resolve(self.store, images)
        if kind in ("comment", "reply", "dynamic"):
            validate_mentions(payload["text"], payload.get("mentions", {}))
            if kind == "dynamic" and payload.get("mentions") and not payload.get("bvid"):
                raise AssistantError("ungrounded_mention", "动态提及需提供已读取视频的 bvid，以核实作者")
        return dict(payload)

    def prepare(self, kind, payload, retry_failed=False, retry_unsent=False):
        run = self.role_run()
        if run.get("read_only"):
            raise AssistantError("action_disabled", "本次为平台只读运行，禁止账号互动")
        payload = self._payload(kind, payload)
        target = payload.get("event_id") or payload.get("bvid") or payload.get("uid")
        if not target:
            content = payload["text"].strip()
            if payload.get("images"):
                content += json.dumps([r["id"] for r in payload["images"]], sort_keys=True)
            target = hashlib.sha256(content.encode()).hexdigest()
        dedupe = f"{run['account']}:{kind}:{target}"
        action_id = uuid.uuid4().hex
        with self.store.lock, self.store.db() as db:
            old = db.execute("SELECT * FROM actions WHERE dedupe=?", (dedupe,)).fetchone()
            if old:
                unsent = old["status"] == "cancelled" or (
                    old["status"] == "prepared" and old["dispatched"] is None)
                replace_unsent = retry_unsent and unsent and old["owner"] == run["owner"]
                if old["status"] == "prepared" and old["generation"] != run["generation"] and not replace_unsent:
                    raise AssistantError("old_preparation", "该动作属于旧运行")
                if not replace_unsent and (old["status"] != "failed" or not retry_failed):
                    return {"id": old["id"], "status": old["status"], "duplicate": True}
                # Keep the old attempt for audit; uncertain/dispatching/succeeded
                # can never release their dedupe key through either retry option.
                db.execute(
                    "UPDATE actions SET dedupe=?,status=? WHERE id=?",
                    (dedupe + ":retired:" + old["id"], "cancelled" if replace_unsent else old["status"], old["id"]),
                )
            db.execute(
                """INSERT INTO actions(id,dedupe,owner,generation,account,kind,payload,status,
                          units,created) VALUES (?,?,?,?,?,?,?,'prepared',?,?)""",
                (
                    action_id,
                    dedupe,
                    run["owner"],
                    run["generation"],
                    run["account"],
                    kind,
                    self.store.ledger_encode("actions.payload", payload),
                    payload.get("units", 1),
                    utcnow(),
                ),
            )
        return {"id": action_id, "status": "prepared"}

    def _eligibility(self, db, run, action, payload, policy):
        if run.get("read_only"):
            raise AssistantError("action_disabled", "本次为平台只读运行，禁止账号互动")
        manifest = self.store.read("manifest")["data"]
        if not run["account"] or run["account"] != manifest["account_uid"]:
            raise AssistantError("account_not_bound", "先在管理入口配置真实账号 UID，再重新启动角色")
        kind = action["kind"]
        action_policy = policy["actions"].get(kind, {"enabled": False, "daily_limit": 0})
        if not action_policy["enabled"]:
            raise AssistantError("action_disabled", f"{kind} 已关闭")
        now = self.clock().astimezone(ZoneInfo(policy["timezone"]))
        day = now.date().isoformat()
        row = db.execute(
            """SELECT COALESCE(SUM(units),0) FROM actions WHERE account=? AND day=?
                AND kind=? AND status IN ('dispatching','succeeded','uncertain')""",
            (run["account"], day, kind),
        ).fetchone()
        if row[0] + action["units"] > action_policy["daily_limit"]:
            raise AssistantError("quota_exhausted", f"{kind} 当日额度不足")
        last = db.execute(
            "SELECT MAX(dispatched) FROM actions WHERE account=?", (run["account"],)
        ).fetchone()[0]
        if (
            last
            and (now - datetime.fromisoformat(last)).total_seconds() < policy["min_action_interval_seconds"]
        ):
            raise AssistantError("interval", "尚未达到动作最小间隔")
        prefs = self.store.read("preferences")["data"]
        if any(
            topic.casefold() in payload.get("text", "").casefold()
            for topic in prefs["blocked_topics"]
            if topic
        ):
            raise AssistantError("blocked_topic", "待发送内容含配置中排除的主题词")
        target_uid = payload.get("uid")
        if "bvid" in payload:
            context = self.store.state(f"context:{payload['bvid']}")
            if not context:
                raise AssistantError("missing_target_context", "无法确认视频的作者与内容范围")
            if kind == "comment" and context["coverage"]["kind"] == "metadata_only":
                raise AssistantError("insufficient_content", "没有实际视频材料，不能生成内容评论")
            target_uid = context.get("owner_uid")
            if any(
                topic.casefold() in context.get("title", "").casefold()
                for topic in prefs["blocked_topics"]
                if topic
            ):
                raise AssistantError("blocked_topic", "视频含配置中排除的主题词")
        if kind in ("reply", "dm_reply", "comment_like"):
            event = db.execute(
                "SELECT * FROM events WHERE id=? AND account=?", (payload["event_id"], run["account"])
            ).fetchone()
            expected_kind = "reply" if kind == "comment_like" else kind
            if not event or event["kind"] != expected_kind:
                raise AssistantError("unknown_event", "只能回复适配器实际收到的相应互动")
            if datetime.fromisoformat(event["received"]) < datetime.fromisoformat(
                run.get("pending_since", run["started_at"])
            ):
                raise AssistantError("old_event", "不追发启动以前的互动")
            payload["_event"] = self.store.ledger_decode("events.body", event["body"])
            target_uid = event["uid"]
            count = db.execute(
                """SELECT COUNT(*) FROM actions WHERE account=? AND day=? AND target_uid=?
                AND kind IN ('reply','dm_reply') AND status IN ('dispatching','succeeded','uncertain')""",
                (run["account"], day, target_uid),
            ).fetchone()[0]
            if kind != "comment_like" and count >= policy["max_reply_turns_per_uid_day"]:
                raise AssistantError("reply_loop_limit", "已达到对该用户的当日回复轮次")
        if set(payload.get("mentions", {}).values()) - {target_uid}:
            raise AssistantError("ungrounded_mention", "只能提及当前视频作者或当前入站互动的发送者")
        if target_uid and (target_uid == run["account"] or target_uid in prefs["blocked_uids"]):
            raise AssistantError("blocked_target", "不向自身或排除的用户执行互动")
        return day, target_uid

    async def action_context(self, payload):
        if "bvid" in payload and not self.store.state(f"context:{payload['bvid']}"):
            await self.video_context(payload["bvid"])

    def record_action(self, recorded):
        from .conversations import record_action
        record_action(self.store, recorded)

    async def video_context(self, bvid, page_index=0, detail=False, start_seconds=0, end_seconds=None,
                            *, continuation=False, gap_request=None):
        run = self.role_run()
        import math

        if (not math.isfinite(start_seconds) or start_seconds < 0
                or end_seconds is not None and (not math.isfinite(end_seconds) or end_seconds <= start_seconds)):
            raise AssistantError("media_window", "补读时间范围无效")
        policy = self.store.read("policy")["data"]
        day = self.clock().astimezone(ZoneInfo(policy["timezone"])).date().isoformat()
        with self.store.lock, self.store.db() as db:
            existing = db.execute(
                "SELECT 1 FROM videos WHERE account=? AND day=? AND bvid=?", (run["account"], day, bvid)
            ).fetchone()
            if continuation:
                from .video_progress import read

                read(self, bvid, page_index)
                existing = db.execute("SELECT 1 FROM videos WHERE account=? AND bvid=?", (run["account"], bvid)).fetchone()
                if not existing:
                    raise AssistantError("unverified_source", "补读需要已计入额度的原视频")
            count = db.execute(
                "SELECT COUNT(*) FROM videos WHERE account=? AND day=?", (run["account"], day)
            ).fetchone()[0]
            if not existing and count >= policy["daily_videos"]:
                raise AssistantError("video_quota", "当日视频处理额度已用完")
            if not continuation:
                db.execute("INSERT INTO videos VALUES (?,?,?) ON CONFLICT DO NOTHING", (run["account"], day, bvid))
        if end_seconds is not None:
            context = await self.adapter.video_context(bvid, page_index, detail=detail,
                                                       start_seconds=start_seconds, end_seconds=end_seconds)
        elif detail or start_seconds:
            context = await self.adapter.video_context(bvid, page_index, detail=detail, start_seconds=start_seconds)
        else:
            context = await self.adapter.video_context(bvid, page_index)
        self.run(run["generation"])
        if gap_request:
            context["gap_request"] = gap_request
        self.store.set_state(f"context:{bvid}", context)
        from .knowledge import snapshot

        source = snapshot(self, context)
        from .video_progress import capture

        context["reading_progress"] = capture(self, context, source)
        self.store.set_state(f"context:{bvid}", context)
        return context

    async def resolve_invitation(self, event):
        from .content_refs import resolve_invitation

        return await resolve_invitation(self, event)

    async def inbox(self, *, archive=True, report=False):
        from companion_core.execution import action_lock

        async with action_lock(self.store, "platform-inbox"):
            return await self._receive_inbox(archive=archive, report=report)

    async def _receive_inbox(self, *, archive=True, report=False):
        run = self.role_run()
        if not run["account"]:
            raise AssistantError("account_not_bound", "尚未绑定账号")
        events = await self.adapter.inbox(run["started_at"])
        self.run(run["generation"])
        inserted = 0
        with self.store.db() as db:
            for event in events:
                if event["uid"] == run["account"]:
                    continue
                inserted += db.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (
                        event["id"],
                        run["account"],
                        event["uid"],
                        event["kind"],
                        event["received"],
                        self.store.ledger_encode("events.body", event),
                    ),
                ).rowcount
        from .connector import record_events

        record_events(self.store, run["account"], events)
        if archive:
            from .conversations import recover_platform_turns

            recover_platform_turns(self)
        from .persona import participant

        persona = self.store.read("persona")["data"]
        events = [{**event, "relationship_context": participant(event, persona)} for event in events]
        return {"events": events, "new_count": inserted} if report else events

    def conversation_context(self, event_id=None):
        from .conversations import context, event_thread, thread_id

        self.mode("role")
        account = self.store.read("manifest")["data"]["account_uid"]
        channel, key = "codex", self.identity()
        events = []
        if event_id:
            with self.store.db() as db:
                row = db.execute("SELECT body FROM events WHERE account=? AND id=?", (account, event_id)).fetchone()
            if not row:
                raise AssistantError("unknown_event", "只能读取本账号实际收到事件所属的对话线程")
            events = [self.store.ledger_decode("events.body", row["body"])]
            channel, key = event_thread(events[0])
        from .persona import state as persona_state

        return {**context(self.store, account, thread_id(account, channel, key)),
                "persona_state": persona_state(self.store, role_chat=not event_id, events=events)}

    def emotion_update(self, proposal, expected_version):
        from .emotions import apply

        # The role-chat endpoint only accepts messages already imported from its host.
        history = self.conversation_context()
        allowed = [item["data"]["id"] for item in history["turns"] if item["data"]["speaker"] == "user"]
        return apply(self, proposal, allowed, expected_version)
