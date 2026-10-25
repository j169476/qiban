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
        if not isinstance(after, str) or type(limit) is not int or not 1 <= limit <= 50:
            raise AssistantError("invalid_query", "事实历史分页参数无效")
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
            new_id = stable_key("fact", "confirmation", identifier, expected, request.actor, reason.strip())
            fact = deepcopy(chosen["data"]["fact"])
            fact.update(valid_from=now, supersedes=supersedes,
                        confirmation={"by": request.actor, "at": now, "reason": reason.strip(), "chosen": identifier})
            data = {**chosen["data"], "id": new_id, "fact": fact,
                    "source": {**chosen["data"]["source"], "type": "user_instruction", "recorded_at": now}}
            self.store.write("memories", data, None, "admin-fact-confirmation:" + request.actor, new_id)
            facts.governed.classify_written("memories", new_id, personal=True,
                                           sources=["memories/" + key for key in supersedes])
            self.admin.repository.audit(request.actor, "fact_confirmation", "memories/" + new_id, db=db)
            return {"id": new_id, "history_preserved": True, "replaced": supersedes}
