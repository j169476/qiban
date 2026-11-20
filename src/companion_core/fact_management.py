"""Authenticated fact timelines and append-only administrative confirmations."""
from copy import deepcopy
from datetime import datetime, timezone

from . import events
from .contracts import Event, stable_key
from .errors import AssistantError
from .governance import GovernedContext
from .temporal_facts import PersonalFacts


class FactManagement:
    def __init__(self, admin, clock=lambda: datetime.now(timezone.utc)):
        self.admin, self.store, self.clock = admin, admin.store, clock

    def view(self, request, identifier):
        self.admin.authority.require(request, administrator=True)
        record = self.store.read("memories", identifier)
        if not identifier.startswith("fact-") or not record["data"].get("fact"):
            raise AssistantError("invalid_query", "请选择一条个人事实记录")
        original = events.get(self.store, record["data"]["source"]["reference"])
        event = Event.from_dict(original["event"])
        governed = GovernedContext(self.store, event)
        facts = PersonalFacts(governed, event, self.clock)
        if not facts.matches(record["data"]["fact"]) or identifier not in {row["id"] for row in facts.headers}:
            raise AssistantError("access_denied", "此事实的原始来源或会话权限已失效")
        return facts

    def history(self, request, identifier, *, after="", limit=20, at=None):
        self.admin.authority.require(request, administrator=True)
        if not isinstance(after, str) or type(limit) is not int or not 1 <= limit <= 50:
            raise AssistantError("invalid_query", "事实历史分页参数无效")
        from .fact_retirement import archived_history
        archived = archived_history(self.store, identifier, after=after, limit=limit, at=at)
        if archived is not None:
            self.admin.authority.require(request, administrator=True)
            return archived
        facts = self.view(request, identifier)
        now = self.clock()
        if at is not None:
            try:
                now = datetime.fromisoformat(at)
                if now.tzinfo is None:
                    raise ValueError
            except (ValueError, TypeError):
                raise AssistantError("invalid_query", "历史时点需要带时区的 ISO 时间") from None
        from .fact_index import statuses

        states = statuses(facts.headers, now)
        headers = sorted(facts.headers, key=lambda h: (datetime.fromisoformat(h["valid_from"]), h["id"]), reverse=True)
        offset = 0
        if after:
            positions = [i for i, row in enumerate(headers) if row["id"] == after]
            if not positions:
                raise AssistantError("edit_conflict", "历史游标已失效，请重新读取第一页")
            offset = positions[0] + 1
        page = headers[offset:offset + limit]
        items = []
        for header in page:
            row = facts.read(header)
            items.append({"id": header["id"], "version": header["version"], **row["data"]["fact"],
                          "status": states[header["id"]], "source": row["data"]["source"]})
        facts.governed.freeze().check()
        facts.check()
        self.admin.authority.require(request, administrator=True)
        return {"items": items, "revision": facts.signature, "total": len(headers),
                "counts": {s: list(states.values()).count(s) for s in ("active", "disputed", "historical", "future")},
                "at": now.isoformat(), "next": page[-1]["id"] if page and offset + limit < len(headers) else None}

    def confirm(self, request, identifier, expected, reason, supersedes):
        return self._append(request, identifier, expected, reason, supersedes)

    def correct(self, request, identifier, expected, reason, supersedes, value):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 160:
            raise AssistantError("invalid_query", "更正后的事实值需要 1 至 160 字")
        return self._append(request, identifier, expected, reason, supersedes, value=value.strip())

    def restore(self, request, identifier, version, expected, reason, supersedes):
        """Copy a prior value of this subject and attribute into a new correction."""
        self.admin.authority.require(request, administrator=True)
        if not isinstance(version, str) or not isinstance(identifier, str):
            raise AssistantError("invalid_query", "请选择要恢复的事实版本")
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            facts = self.view(request, identifier)
            if facts.signature != expected:
                raise AssistantError("edit_conflict", "事实已变化，请重新读取")
            headers = {row["id"]: row for row in facts.headers}
            old = headers.get(version)
            if (old is None or old["attribute"] != headers[identifier]["attribute"]
                    or facts.states[version] not in {"historical", "disputed", "active"}):
                raise AssistantError("invalid_query", "该版本不属于这条事实的可用历史")
            value = facts.read(old)["data"]["fact"]["value"]
            facts.governed.freeze().check()
            return self.correct(request, identifier, expected, reason, supersedes, value)

    def _append(self, request, identifier, expected, reason, supersedes, *, value=None):
        self.admin.authority.require(request, administrator=True)
        if (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000
                or not isinstance(supersedes, list) or not supersedes or len(supersedes) > 20
                or any(not isinstance(key, str) for key in supersedes) or len(set(supersedes)) != len(supersedes)
                or identifier not in supersedes):
            raise AssistantError("invalid_query", "确认需要原因及最多 20 条当前记录，包含选中项")
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            facts = self.view(request, identifier)
            if facts.signature != expected:
                raise AssistantError("edit_conflict", "事实或权限在查看后已经变化，请刷新时间线")
            headers = {row["id"]: row for row in facts.headers}
            chosen = facts.read(headers[identifier])
            if value is not None and value == chosen["data"]["fact"]["value"]:
                raise AssistantError("invalid_query", "新值与选中事实相同，请使用确认现有事实")
            attribute = chosen["data"]["fact"]["attribute"]
            if any(key not in headers or headers[key]["attribute"] != attribute
                   or facts.states[key] not in {"active", "disputed"} for key in supersedes):
                raise AssistantError("invalid_query", "只能确认同一属性的当前或冲突记录；未来与历史记录不能提前生效")
            for key in supersedes:
                facts.read(headers[key])
            facts.governed.freeze().check()
            facts.check()
            self.admin.authority.require(request, administrator=True)
            now = self.clock().isoformat()
            operation = "correction" if value is not None else "confirmation"
            new_id = stable_key("fact", operation, identifier, expected, request.actor, reason.strip(),
                                *([value] if value is not None else []))
            fact = deepcopy(chosen["data"]["fact"])
            fact.update(valid_from=now, supersedes=supersedes,
                        confirmation={"by": request.actor, "at": now, "reason": reason.strip(), "chosen": identifier})
            data = {**chosen["data"], "id": new_id, "fact": fact,
                    "source": {**chosen["data"]["source"], "type": "user_instruction", "recorded_at": now}}
            if value is not None:
                fact.pop("confirmation", None)
                fact["correction"] = {"by": request.actor, "at": now, "reason": reason.strip(),
                                      "original": identifier, "original_value": fact["value"]}
                fact["value"] = value
                data["content"] = "管理员更正：" + value
            self.store.write("memories", data, None, "admin-fact-" + operation + ":" + request.actor, new_id)
            facts.governed.classify_written("memories", new_id, personal=True,
                                           sources=["memories/" + key for key in supersedes])
            self.admin.repository.audit(request.actor, "fact_" + operation, "memories/" + new_id, db=db)
            return {"id": new_id, "history_preserved": True, "replaced": supersedes}
