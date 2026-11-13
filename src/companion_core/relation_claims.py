"""Administrator-only adjudication of already-existing interpersonal claims.

A claim is ``subject``/``object``/``relation``/``declarer``/``evidence``: it is
neither an ``identity_links`` "same person" link nor a platform friendship state.
This page therefore only changes the status of a claim that already exists:

* no claim is proposed, and the statement, subject, object, declarer and original
  evidence are never rewritten;
* the status set stays the core's ``claimed``/``confirmed``/``disputed``/
  ``revoked``; a revoked claim can still be confirmed or disputed again exactly as
  the core already allowed, and the preview names that as restoring the claim,
  without re-pinning sources, reviving an invalidated source or restoring an old
  dependent version;
* ``RelationService.adjudicate`` stays the only writer, so the older web entry
  point keeps the same semantics and now keeps the same history;
* one immutable encrypted history row (``relation_history``) is appended in the
  same transaction as the claim status, the claim policy and the desktop request
  receipt; nothing is deleted automatically and no growing JSON array is used;
* account links, reply authorizations and every other record permission are not
  touched, and this module never issues a model capability.

Every change is preview -> commit. The preview returns a server-side, short-lived
token bound to the management session, the claim's complete status/version, the
claim policy's version and validity, the evidence pins, the source report and the
dependency fingerprint. The commit re-reads all of it inside the caller's
transaction and refuses a stale token; a status/valid pair that was already
inconsistent is reported instead of being silently repaired.

Evidence text is shown apart from the original reference: an item is quoted only
when its pinned version still equals the current version. A changed version, a
deleted event or an unsupported kind is named as such and its new text is not
substituted for the old one.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import replace

from . import relation_history, source_report
from .access import AccessService
from .contracts import ActorRef
from .errors import AssistantError
from .record_inspection import impact
from .record_sharing import actor_label
from .relations import ANOMALY_DETAIL, STATUSES, TARGETS, RelationService, inconsistency

STATUS_LABELS = {
    "claimed": "仅声明（尚未核对）",
    "confirmed": "已确认",
    "disputed": "有争议",
    "revoked": "已撤销",
}
STATUS_MEANING = ("“仅声明”是声明人自己提交、尚未核对；“有争议”仍会由核心带状态返回；"
                  "“已撤销”不再由核心关系查询返回，但声明本身与它的历史仍然保留。")
ANOMALY_DETAIL = dict(ANOMALY_DETAIL)
PAGE_MAX = 50
HISTORY_MAX = relation_history.PAGE_MAX
EVIDENCE_MAX = 16
EXCERPT_MAX = 2000
EXCERPT_PAGE_MAX = 12000
PREVIEW_SECONDS = 600
MAX_PREVIEWS = 64
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}")
MEANING = (
    "这里只更正已有关系声明的状态。它不是“同一人”的账号关联，也不是好友或群关系；"
    "不会新增关系，不改正文、主体、声明人和原证据，也不改同一人的账号关联、回复授权或其他资料权限。"
    "状态变化只更新这条声明自身的权限版本与由状态决定的“有效”标记。"
)

TEXT_FIELDS = ("content", "text", "summary")


def _fail(code, message):
    raise AssistantError(code, message)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def _policy_state(policy):
    """Everything a preview promises to keep, for the commit re-check."""
    return [policy.resource, policy.version, policy.classification, sorted(policy.owners),
            policy.conversation, policy.audience, list(policy.sources),
            [list(pin) for pin in policy.source_versions], bool(policy.valid), policy.expires_at]


def _claim_state(row):
    return [row["id"], row["subject"], row["object"], row["declarer"], row["status"],
            row["version"], row["body"]]


class RelationClaims:
    """Preview/commit claim adjudication bound to one management instance."""

    def __init__(self, admin, actor=None, conversation=None):
        self.admin = admin
        self.store = admin.store
        self.repository = admin.repository
        self.authority = admin.authority
        self.access = admin.access
        # The owner's own sealed identity, used only for a source check when the
        # claim really belongs to it; never offered as a grant or a claim target.
        self.actor = actor
        # The exact conversation the desktop management entry point issues its
        # sealed request with, so no other sealed administrator context can drive
        # this page.
        self.conversation = conversation
        # Restarting the app builds a new instance, so every token dies with it.
        self.previews = {}
        self.relations = RelationService(self.repository, self.authority)

    # --- read-only helpers ---------------------------------------------------

    def _require(self, request):
        self.authority.require(request, administrator=True)
        if self.conversation is not None and request.conversation != self.conversation:
            _fail("access_denied", "请求身份、授权或管理会话已失效")
        if getattr(self.store, "backend", None) != "postgres":
            _fail("relation_backend", "人际关系声明管理需要事务化 PostgreSQL 核心")

    def _identifier(self, identifier):
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            _fail("relation_claim", "关系声明标识格式无效，请重新选择。")
        return identifier

    def _body(self, row):
        try:
            body = self.store.ledger_decode("relationship_claims.body", row["body"])
        except AssistantError:
            return None, "这条声明的内容无法解读（完整性校验失败），本页不推测它的正文。"
        if not isinstance(body, dict):
            return None, "这条声明的内容格式无法识别，本页不推测它的正文。"
        return body, None

    def _labels(self, db, keys):
        """Only registration that really exists is turned into a readable label."""
        wanted = sorted({key for key in keys if isinstance(key, str) and key})
        if not wanted:
            return {}
        rows = db.execute("SELECT actor_key,identity_body FROM actor_nodes WHERE actor_key=ANY(?)",
                          (wanted,)).fetchall()
        labels = {}
        for row in rows:
            try:
                actor = ActorRef(**self.store.ledger_decode("actor_nodes.identity", row["identity_body"]))
            except (AssistantError, KeyError, TypeError, ValueError):
                continue
            if actor.key == row["actor_key"]:
                labels[row["actor_key"]] = actor_label(actor)
        return labels

    def _account(self, key, labels):
        label = labels.get(key)
        # No identity is invented for an unregistered key: only an opaque id.
        return {"key": key, "registered": label is not None,
                "label": label or ("未登记账号（" + str(key or "")[:8] + "…）")}

    def _registered_actor(self, key):
        with self.store.db() as db:
            row = db.execute("SELECT identity_body FROM actor_nodes WHERE actor_key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            actor = ActorRef(**self.store.ledger_decode("actor_nodes.identity", row["identity_body"]))
        except (AssistantError, KeyError, TypeError, ValueError):
            return None
        return actor if actor.key == key else None

    def _policy_view(self, policy):
        """Metadata only: the claim body is never part of this view."""
        pins = list(policy.source_versions)
        parsed = dict(policy.source_versions)
        return {"classification": policy.classification, "owners": sorted(policy.owners),
                "conversation": policy.conversation, "audience": policy.audience,
                "valid": bool(policy.valid), "expires_at": policy.expires_at,
                "version": policy.version, "source_count": len(policy.sources),
                "sources": list(policy.sources[:EVIDENCE_MAX]),
                "source_versions": [[source, version] for source, version in pins[:EVIDENCE_MAX]],
                "pins_complete": (len(parsed) == len(policy.source_versions)
                                  and set(parsed) == set(policy.sources))}

    def _claim_row(self, db, identifier, *, lock=False):
        return db.execute("SELECT id,subject,object,declarer,status,version,body "
                          "FROM relationship_claims WHERE id=?" + (" FOR UPDATE" if lock else ""),
                          (identifier,)).fetchone()

    def _claim_view(self, row, labels):
        body, error = self._body(row)
        evidence = body.get("evidence") if body else None
        view = {"id": row["id"], "cursor": row["id"], "status": row["status"],
                "status_label": STATUS_LABELS.get(row["status"], row["status"]),
                "version": row["version"], "queryable": row["status"] != "revoked",
                "restorable": row["status"] == "revoked",
                "subject": self._account(row["subject"], labels),
                "object": self._account(row["object"], labels),
                "declarer": self._account(row["declarer"], labels),
                "relation": body.get("relation") if body else None,
                "direction": body.get("direction") if body else None,
                "declared_reason": body.get("reason") if body else None,
                "evidence_count": len(evidence) if isinstance(evidence, list) else 0}
        if error:
            view["body_error"] = error
        return view

    # --- paged reads ---------------------------------------------------------

    def listing(self, request, after, limit, status):
        self._require(request)
        if not isinstance(after, str) or len(after) > 80 or (after and not IDENTIFIER.fullmatch(after)):
            _fail("relation_query", "分页位置无效，请重新读取。")
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            _fail("relation_query", f"每次只能查看 1 至 {PAGE_MAX} 条声明。")
        if not isinstance(status, str) or status not in ("all",) + STATUSES:
            _fail("relation_query", "状态筛选无效，请重新选择。")
        where, args = ("", []) if status == "all" else (" AND status=?", [status])
        with self.store.db() as db:
            rows = db.execute("SELECT id,subject,object,declarer,status,version,body "
                              "FROM relationship_claims WHERE id>?" + where
                              + " ORDER BY id LIMIT ?", (after, *args, limit + 1)).fetchall()
            page = rows[:limit]
            labels = self._labels(db, {key for row in page
                                       for key in (row["subject"], row["object"], row["declarer"])})
            totals = {row["status"]: row["count"] for row in db.execute(
                "SELECT status,COUNT(*) AS count FROM relationship_claims GROUP BY status").fetchall()}
        claims = [self._claim_view(row, labels) for row in page]
        return {"claims": claims, "count": len(claims),
                "next": page[-1]["id"] if len(rows) > limit else None,
                "after": after, "limit": limit, "page_max": PAGE_MAX, "status": status,
                "totals": {value: totals.get(value, 0) for value in STATUSES},
                "statuses": [{"value": value, "label": STATUS_LABELS[value]} for value in STATUSES],
                "status_meaning": STATUS_MEANING, "meaning": MEANING}

    def detail(self, request, identifier):
        self._require(request)
        identifier = self._identifier(identifier)
        resource = "relation/" + identifier
        with self.store.db() as db:
            row = self._claim_row(db, identifier)
            if row is None:
                _fail("relation_claim", "找不到这条关系声明，请重新读取。")
            labels = self._labels(db, {row["subject"], row["object"], row["declarer"]})
        statistics = relation_history.bounds(self.store, identifier)
        claim = self._claim_view(row, labels)
        policy = self.repository.policy(resource)
        body, _error = self._body(row)
        anomaly = inconsistency(row["status"], policy)
        report = self._source_view(self._source_check(resource, root=policy), row["status"])
        affected = impact(self.store, resource)
        return {"claim": claim, "policy": None if policy is None else self._policy_view(policy),
                "evidence": self._evidence(body, policy), "source": report,
                "impact": self._impact_view(affected),
                "history_available": statistics["count"] > 0,
                "history": self._history_view(row, statistics),
                "readable": self._readable(policy, row["declarer"]),
                "check": {"consistent": anomaly is None, "anomaly": anomaly,
                          "detail": ANOMALY_DETAIL.get(anomaly)},
                "statuses": [{"value": value, "label": STATUS_LABELS[value]} for value in STATUSES],
                "targets": [{"value": value, "label": STATUS_LABELS[value]} for value in TARGETS],
                "status_meaning": STATUS_MEANING, "evidence_limit": EVIDENCE_MAX,
                "excerpt_limit": EXCERPT_MAX, "excerpt_page_limit": EXCERPT_PAGE_MAX,
                "history_limit": HISTORY_MAX, "meaning": MEANING}

    def history(self, request, identifier, after, limit):
        self._require(request)
        identifier = self._identifier(identifier)
        with self.store.db() as db:
            row = self._claim_row(db, identifier)
            if row is None:
                _fail("relation_claim", "找不到这条关系声明，请重新读取。")
        page = relation_history.page(self.store, identifier, after, limit)
        actors = {item.get("actor") for item in page["items"]
                  if isinstance(item.get("actor"), str)}
        with self.store.db() as db:
            labels = self._labels(db, actors)
        items = [self._history_item(item, labels) for item in page["items"]]
        statistics = relation_history.bounds(self.store, identifier)
        return {"identifier": identifier, "items": items, "next": page["next"],
                "count": page["count"], "after": page["after"], "limit": page["limit"],
                "history": self._history_view(row, statistics),
                "statuses": [{"value": value, "label": STATUS_LABELS[value]} for value in STATUSES],
                "meaning": ("这里按写入顺序列出每一次状态更正，每条不可变；"
                            "不包含早于本功能开始记录之前的裁决，也不推测那时的内容。")}

    def _history_item(self, entry, labels):
        if entry.get("unreadable"):
            return {"cursor": entry.get("cursor"), "unreadable": True,
                    "detail": entry.get("detail")}
        before, after = entry.get("before") or {}, entry.get("after") or {}
        actor = entry.get("actor")
        return {"cursor": entry.get("cursor"), "sequence": entry.get("sequence"),
                "at": entry.get("at"), "unreadable": False,
                "actor": actor,
                "actor_label": labels.get(actor) or ("未登记管理员（" + str(actor or "")[:8] + "…）"),
                "reason": entry.get("reason") or "",
                "restored": bool(entry.get("restored")),
                "before": {"status": before.get("status"),
                           "status_label": STATUS_LABELS.get(before.get("status"), before.get("status")),
                           "version": before.get("version"),
                           "policy_version": before.get("policy_version"),
                           "valid": before.get("valid")},
                "after": {"status": after.get("status"),
                          "status_label": STATUS_LABELS.get(after.get("status"), after.get("status")),
                          "version": after.get("version"),
                          "policy_version": after.get("policy_version"),
                          "valid": after.get("valid")},
                "claim_snapshot": self._snapshot_view(entry.get("claim_snapshot")),
                "sources": [[source, version] for source, version in (entry.get("sources") or [])]}

    def _snapshot_view(self, snapshot):
        if not isinstance(snapshot, dict):
            return None
        body = snapshot.get("body") if isinstance(snapshot.get("body"), dict) else {}
        return {"subject": snapshot.get("subject"), "object": snapshot.get("object"),
                "declarer": snapshot.get("declarer"), "status": snapshot.get("status"),
                "version": snapshot.get("version"),
                "relation": body.get("relation"), "declared_reason": body.get("reason"),
                "evidence": body.get("evidence") if isinstance(body.get("evidence"), list) else [],
                "unreadable": bool(body.get("unreadable"))}

    def _history_view(self, row, statistics):
        """Say honestly where the record starts instead of inventing earlier verdicts."""
        count = statistics["count"]
        first = statistics["first"] if isinstance(statistics["first"], dict) else None
        if count == 0:
            return {"available": False, "count": 0,
                    "complete": bool(row["status"] == "claimed"),
                    "note": ("这条声明还没有本页记录的裁决；它当前是“仅声明”。" if row["status"] == "claimed"
                             else "这条声明当前的“" + STATUS_LABELS.get(row["status"], row["status"])
                             + "”发生在本次管理功能开始记录之前，因此早先的裁决没有记录；"
                               "本页只从本次管理开始有记录，不虚构以前的裁决。")}
        started = None if first is None or first.get("unreadable") else (first.get("before") or {}).get("status")
        complete = started == "claimed"
        last = statistics["last"] if isinstance(statistics["last"], dict) else None
        current = bool(not last or last.get("unreadable") or
                       ((last.get("after") or {}).get("version") == row["version"]
                        and (last.get("after") or {}).get("status") == row["status"]))
        note = "这里共记录了 " + str(count) + " 次状态更正。"
        if not complete:
            note += ("最早一次记录的变更前状态不是“仅声明”，说明更早的裁决发生在本次管理功能开始记录之前；"
                     "本页不虚构那些裁决。")
        if not current:
            note += "当前声明的版本与最近一次记录的版本不一致，说明期间还有其他入口或恢复流程改动过它。"
        return {"available": True, "count": count, "complete": complete,
                "current": current, "note": note}

    # --- source diagnostics and impact ---------------------------------------

    def _source_check(self, resource, root=None):
        """Bounded check of the claim's *whole* source closure.

        The closure covers direct and indirect sources (cycle, depth, missing
        policy, rotated pin, revoked and expired nodes, truncated budget), and it
        returns the exact state of every visited node alongside the wording, so a
        second rotation of the same source can never look identical to the first.
        ``root`` substitutes only the claim's own policy, which lets a preview
        describe the state it would produce; every source is always read as is.
        """
        return source_report.closure(self.repository, self.access.limits, resource, root=root)

    def _source_view(self, check, status):
        """Display view: the stable report plus the claim's own current status."""
        report = dict(check["report"])
        report.update({"digest": source_report.digest(check), "claim_status": status,
                       "queryable": status != "revoked",
                       "meaning": ("这里核对的是这条声明自身、它的直接来源以及这些来源再往下的整条来源链；"
                                   "这条声明自身的有效状态按本次核对的状态给出，来源一律按当前状态核对。"
                                   "这些只是来源与有效状态的检查结果，不是“谁一定能读到”的结论；"
                                   "状态更正不会让已更换或已失效的来源重新可读。")})
        return report

    def _require_complete_sources(self, check):
        if not check["complete"]:
            _fail("relation_source_limit", "来源检查未能完整完成，请先检查来源记录或整理过大的来源链，再重新预览。此次没有更改状态。")

    def _impact_view(self, affected):
        total = affected["affected_count"]
        return {**affected, "total_count": total,
                # ``impact`` always seeds its closure with the record itself.
                "self_count": 1, "downstream_count": max(0, total - 1),
                "meaning": ("影响范围共 " + str(total) + " 项，其中 1 项是这条声明自身，另有 "
                            + str(max(0, total - 1)) + " 项是以它为来源的下游资料；"
                            "这里只统计数量与标识，不推断其余资料各自能否读取。")}

    def _readable(self, policy, declarer):
        """Whether the declarer's own reply can still use this claim as evidence.

        This is a real ``AccessService`` check with a sealed request, reported
        apart from "the status change succeeded"; it is never inferred from the
        requested status.
        """
        if policy is None:
            return {"checked": False, "allowed": None, "actor": None,
                    "note": "这条声明的权限记录缺失，无法核对来源是否允许读取。"}
        actor = self._registered_actor(declarer)
        if actor is None:
            return {"checked": False, "allowed": None, "actor": None,
                    "note": ("声明人没有可核对的登记身份，因此没有构造任何账号身份，"
                             "也没有核对来源是否允许读取。")}
        conversation = policy.conversation or "relation-claim-check"
        if policy.audience == "private":
            request = self.authority.issue(actor=actor, conversation=conversation, audience="private",
                                           recipients=(actor,), purpose="reply", lifetime=60)
        else:
            request = self.authority.issue(actor=actor, conversation=conversation,
                                           audience=policy.audience, purpose="reply", lifetime=60)
        reader = AccessService(self.repository, self.authority, limits=self.access.limits)
        try:
            allowed = reader.can_read(request, policy.resource)
        except AssistantError as error:
            if error.code != "access_denied":
                raise
            allowed = False
        return {"checked": True, "allowed": allowed, "actor": actor_label(actor),
                "note": ("按来源与有效状态核对：声明人自己的回复仍可以把它作为依据。" if allowed else
                         "按来源与有效状态核对：声明人自己的回复现在也不能把它作为依据"
                         "（状态已撤销、来源已更换或失效，或分类被隔离）。")}

    # --- evidence ------------------------------------------------------------

    def _evidence(self, body, policy):
        """At most ``EVIDENCE_MAX`` items, each matched against its pinned version.

        Only a pinned version that still equals the current version is quoted.
        Everything else is named: a changed event, a deleted one, an unsupported
        kind or an unreadable payload never gets its new text presented as the
        old evidence.
        """
        pins = dict(policy.source_versions) if policy is not None else {}
        declared = [item for item in (body or {}).get("evidence") or [] if isinstance(item, str)]
        ordered = list(dict.fromkeys([*declared, *(source for source, _ in pins.items())]))[:EVIDENCE_MAX]
        items, used = [], 0
        for resource in ordered:
            item = self._evidence_item(resource, pins.get(resource))
            text = item.pop("text")
            if isinstance(text, str):
                remaining = EXCERPT_PAGE_MAX - used
                if len(text) > EXCERPT_MAX:
                    item["truncated"] = True
                    text = text[:EXCERPT_MAX]
                if len(text) > remaining:
                    item["page_limited"] = True
                    item["excerpt"] = None
                    item["state_label"] += "；本页摘录总量已达上限，这一项的正文没有展示"
                else:
                    item["excerpt"] = text
                    used += len(text)
            else:
                item["excerpt"] = None
            items.append(item)
        return {"items": items, "count": len(items), "limit": EVIDENCE_MAX,
                "excerpt_chars": used, "excerpt_length": EXCERPT_MAX,
                "excerpt_total": EXCERPT_PAGE_MAX,
                "declared_count": len(declared), "pinned_count": len(pins),
                "meaning": ("只有“引用版本与当前一致”的来源才展示正文摘录；"
                            "已更换、已删除、无法解读或不支持的类型只说明状态，不用现在的正文冒充当时的证据。")}

    def _evidence_item(self, resource, pin):
        kind, _, identifier = resource.partition("/")
        item = {"resource": resource, "kind": kind, "pinned_version": pin, "current_version": None,
                "state": "unsupported", "state_label": "这一类型不提供文本摘录",
                "matched": False, "truncated": False, "page_limited": False, "text": None}
        if not pin:
            item["state"] = "unpinned"
            item["state_label"] = "声明没有记录这一项的引用版本，因此无法核对当时的原文"
            return item
        if kind == "event":
            with self.store.db() as db:
                row = db.execute("SELECT version,deleted,body FROM core_events WHERE id=?",
                                 (identifier,)).fetchone()
            if row is None or row[1]:
                item["state"] = "missing"
                item["state_label"] = "引用的消息记录已不存在或已删除，本页不重建原文"
                return item
            item["current_version"] = row["version"]
            if row["version"] != pin:
                item["state"] = "changed"
                item["state_label"] = "当前版本已更换，不是当初引用的版本；本页不展示新正文"
                return item
            try:
                data = self.store.ledger_decode("core_events.body", row["body"])
            except AssistantError:
                item["state"] = "unreadable"
                item["state_label"] = "引用的消息内容无法解读（完整性校验失败）"
                return item
            return self._matched(item, data.get("text") if isinstance(data, dict) else None)
        if kind in getattr(self.store.profile, "collections", ()):
            try:
                record = self.store.read(kind, identifier)
            except AssistantError as error:
                if error.code in ("not_found", "invalid_path", "missing_id", "invalid_id", "id_mismatch"):
                    item["state"] = "missing"
                    item["state_label"] = "引用的记录已不存在，本页不重建原文"
                else:
                    item["state"] = "unreadable"
                    item["state_label"] = "引用的记录无法解读（" + error.code + "）"
                return item
            item["current_version"] = record["version"]
            if record["version"] != pin:
                item["state"] = "changed"
                item["state_label"] = "当前版本已更换，不是当初引用的版本；本页不展示新正文"
                return item
            data = record["data"] if isinstance(record["data"], dict) else {}
            text = next((data[name] for name in TEXT_FIELDS
                         if isinstance(data.get(name), str) and data[name].strip()), None)
            return self._matched(item, text)
        return item

    def _matched(self, item, text):
        item["matched"] = True
        if isinstance(text, str) and text:
            item["state"] = "matched"
            item["state_label"] = "引用版本与当前一致"
            item["text"] = text
            return item
        item["state"] = "no_text"
        item["state_label"] = "引用版本与当前一致，但这一项没有可展示的正文"
        return item

    # --- preview tokens ------------------------------------------------------

    def _prune(self):
        now = time.monotonic()
        for token in [token for token, pending in self.previews.items() if pending["expires"] <= now]:
            self.previews.pop(token, None)

    def _remember(self, state):
        self._prune()
        while len(self.previews) >= MAX_PREVIEWS:
            soonest = min(self.previews, key=lambda token: self.previews[token]["expires"])
            self.previews.pop(soonest, None)
        token = secrets.token_urlsafe(24)
        self.previews[token] = state
        return token

    def _entry(self, request, token):
        self._require(request)
        if not isinstance(token, str) or not token:
            _fail("relation_preview_expired", "请重新预览后再确认。")
        self._prune()
        pending = self.previews.get(token)
        if (not pending or pending["session"] != request.session_id
                or pending["expires"] <= time.monotonic()):
            _fail("relation_preview_expired", "预览已过期或管理会话已改变，请重新预览。")
        return pending

    # --- preview -------------------------------------------------------------

    def preview(self, request, identifier, status, reason):
        self._require(request)
        identifier = self._identifier(identifier)
        if not isinstance(status, str) or status not in TARGETS:
            _fail("relation_status", "关系状态只能选择已确认、有争议或已撤销。")
        if not isinstance(reason, str) or len(reason) > relation_history.REASON_MAX:
            _fail("relation_reason", f"更正说明最多 {relation_history.REASON_MAX} 字。")
        reason = reason.strip()
        resource = "relation/" + identifier
        with self.store.db() as db:
            row = self._claim_row(db, identifier)
            if row is None:
                _fail("relation_claim", "找不到这条关系声明，请重新读取。")
            labels = self._labels(db, {row["subject"], row["object"], row["declarer"]})
            policy = self.repository.policy(resource)
            anomaly = inconsistency(row["status"], policy)
            if anomaly is not None:
                _fail("relation_inconsistent", ANOMALY_DETAIL[anomaly]
                      + "本页不会默默把它改回有效；请先由维护机制核对这条声明与它的来源。")
            if row["status"] == status:
                _fail("relation_status", "新状态与当前状态相同，本页不会为同状态增加版本。")
            claim = self._claim_view(row, labels)
            # The claim's own validity is checked as the *after* state, because that
            # is what this preview promises; every source is checked as it is now.
            check = self._source_check(resource, root=replace(policy, valid=status != "revoked"))
            self._require_complete_sources(check)
            report = self._source_view(check, status)
            affected = impact(self.store, resource)
            statistics = relation_history.bounds(self.store, identifier)
            now = time.monotonic()
            state = {"session": request.session_id, "expires": now + PREVIEW_SECONDS,
                     "identifier": identifier, "resource": resource,
                     "claim_version": row["version"], "claim_status": row["status"],
                     "claim": _digest(_claim_state(row)),
                     "policy_version": policy.version, "policy": _digest(_policy_state(policy)),
                     "sources": source_report.digest(check), "impact": affected["fingerprint"],
                     "payload": {"status": status, "reason": reason}}
        token = self._remember(state)
        restored = row["status"] == "revoked"
        before = {"status": row["status"], "status_label": STATUS_LABELS[row["status"]],
                  "version": row["version"], "policy_version": policy.version,
                  "valid": bool(policy.valid), "queryable": row["status"] != "revoked"}
        after = {"status": status, "status_label": STATUS_LABELS[status],
                 "valid": status != "revoked", "queryable": status != "revoked",
                 "restores": restored and status != "revoked"}
        if status == "revoked":
            notice = ("确认后只把这条声明标记为“已撤销”：核心关系查询不再返回它；"
                      "正文、主体、声明人、原证据、账号关联、回复授权和其他资料权限保持原样。")
        elif restored:
            notice = ("确认后恢复此声明（已撤销 → " + STATUS_LABELS[status] + "）："
                      "只改这一条声明的状态与其自身的权限版本；"
                      "已失效或已更换的来源不会因此重新可读，来源版本不会被重新固定，"
                      "旧依赖版本也不会自动恢复。")
        else:
            notice = ("确认后只改变这条声明的状态（" + STATUS_LABELS[row["status"]] + " → "
                      + STATUS_LABELS[status] + "）：不新增关系，不改正文、主体、声明人和原证据，"
                      "也不改同一人的账号关联、回复授权或其他资料权限。")
        return {"token": token, "action": "adjudicate", "identifier": identifier,
                "resource": resource, "claim": claim,
                "before": before, "after": after, "restored": restored,
                "reason": reason, "reason_limit": relation_history.REASON_MAX,
                "source": report, "impact": self._impact_view(affected),
                "readable_before": self._readable(policy, row["declarer"]),
                "history": self._history_view(row, statistics),
                "status_meaning": STATUS_MEANING, "notice": notice, "written": False}

    # --- commit / cancel -----------------------------------------------------

    def cancel(self, request, token):
        pending = self._entry(request, token)
        self.previews.pop(token, None)
        return {"cancelled": True, "written": False, "identifier": pending["identifier"]}

    def commit(self, request, token):
        self._require(request)
        self._entry(request, token)
        pending = self.previews.pop(token, None)
        identifier = pending["identifier"]
        resource = pending["resource"]
        status = pending["payload"]["status"]
        try:
            with self.store.lock, self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                self.authority.require(request, administrator=True)
                row = self._claim_row(db, identifier, lock=True)
                if row is None:
                    _fail("edit_conflict", "关系声明在预览后已不存在，请重新预览。")
                if (row["version"] != pending["claim_version"]
                        or row["status"] != pending["claim_status"]):
                    _fail("edit_conflict", "关系声明在预览后已变化，请重新预览。")
                if _digest(_claim_state(row)) != pending["claim"]:
                    _fail("edit_conflict", "关系声明在预览后已变化，请重新预览。")
                policy = self.repository.policy(resource)
                if policy is None:
                    _fail("relation_inconsistent", "这条声明的权限记录在预览后缺失，请重新读取后再确认。")
                if (policy.version != pending["policy_version"]
                        or _digest(_policy_state(policy)) != pending["policy"]):
                    _fail("edit_conflict", "这条声明的权限在预览后已变化，请重新预览。")
                # Same closure and same root substitution as the preview, so the
                # comparison is about state, never about a read timestamp.
                check = self._source_check(resource, root=replace(policy,
                                                                 valid=status != "revoked"))
                self._require_complete_sources(check)
                if source_report.digest(check) != pending["sources"]:
                    _fail("edit_conflict", "这条声明的来源或有效期在预览后已变化，请重新预览。")
                affected = impact(self.store, resource)
                if affected["fingerprint"] != pending["impact"]:
                    _fail("edit_conflict", "这条声明的依赖影响在预览后已变化，请重新预览。")
                written = self.relations.adjudicate(request, identifier, status,
                                                    pending["claim_version"], pending["payload"]["reason"])
                self.repository.audit(request.actor, "desktop_relation_adjudicate", resource,
                                      pending["claim_version"], written["version"], db=db)
                after_policy = self.repository.policy(resource)
                readable = self._readable(after_policy, row["declarer"])
                # The returned diagnostics must describe the state that now exists, not
                # the pre-write one: a revocation has to read as revoked immediately.
                after_check = self._source_check(resource, root=after_policy)
                after_report = self._source_view(after_check, status)
        except BaseException:
            # A refusal inside this transaction kept nothing, so the same preview
            # may be retried after the administrator re-reads the claim. A failure
            # after this returns (for example the outer request receipt) is outside
            # this scope: the token stays consumed.
            self.previews[token] = pending
            raise
        return {"action": "adjudicate", "identifier": identifier, "resource": resource,
                "status": status, "status_label": STATUS_LABELS[status],
                "version": written["version"], "changed": True,
                "state_changed": True,
                "before": {"status": pending["claim_status"],
                           "status_label": STATUS_LABELS[pending["claim_status"]],
                           "version": pending["claim_version"],
                           "policy_version": pending["policy_version"]},
                "after": {"status": status, "status_label": STATUS_LABELS[status],
                          "version": written["version"],
                          "policy_version": None if after_policy is None else after_policy.version,
                          "valid": None if after_policy is None else bool(after_policy.valid),
                          "queryable": status != "revoked"},
                "restored": written["restored"], "history": written["history"],
                "reason": pending["payload"]["reason"],
                "source": after_report, "readable": readable, "usable": readable["allowed"],
                "impact": self._impact_view(affected),
                "notice": ("状态更改成功：" + STATUS_LABELS[pending["claim_status"]] + " → "
                           + STATUS_LABELS[status] + "，并已写入一条不可变的裁决记录。"
                           + (" 已恢复这条声明；已失效或已更换的来源没有因此重新可读，"
                              "来源版本未被重新固定，旧依赖版本也没有自动恢复。" if written["restored"] else
                              " 正文、主体、声明人、原证据、账号关联、回复授权和其他资料权限保持原样。")),
                "readable_notice": readable["note"]}
