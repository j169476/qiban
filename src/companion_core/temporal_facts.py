"""Immutable, source-bound personal assertions and a conservative current-fact view."""
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import fact_index
from .contracts import stable_key
from .errors import AssistantError
from .schema import obj

ATTRIBUTES = ("residence", "preferred_name", "drink_preference", "food_preference")
TEXT = {"type": "string", "minLength": 1, "maxLength": 160}
UPDATE = obj({"attribute": {"enum": list(ATTRIBUTES)}, "value": TEXT,
              "quote": {"type": "string", "minLength": 1, "maxLength": 500},
              "valid_from": {"anyOf": [{"type": "null"}, {"type": "string", "format": "date"}]},
              "supersedes": {"type": "array", "maxItems": 20, "uniqueItems": True,
                             "items": {"type": "string", "maxLength": 80}}})
FACT = obj({"actor": TEXT, "account": {"type": "string", "minLength": 1, "maxLength": 200},
            "conversation": {"type": "string", "maxLength": 500},
            "attribute": UPDATE["properties"]["attribute"], "value": TEXT,
            "quote": UPDATE["properties"]["quote"], "valid_from": {"type": "string", "format": "date-time"},
            "supersedes": UPDATE["properties"]["supersedes"]})
FACT["properties"]["confirmation"] = obj({"by": TEXT, "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
    "at": {"type": "string", "format": "date-time"}, "chosen": TEXT})
UPDATES = {"type": "array", "maxItems": 3, "items": UPDATE}
PROMPT = (
    "personal_facts 是程序按本会话来源计算的人物事实：active 为当前有效，historical 为旧状态，"
    "disputed 表示冲突未解决，future 尚未生效。普通记忆或关系 facts 不能覆盖其明确的更正。"
    "只有当前用户明确讲述自己的居住地、希望的称呼、饮食偏好，才可返回 fact_updates（最多3项）。"
    "quote 必须是当前 event.text 的完整原话片段，value 必须逐字出现在 quote；转述、假设、提问不更新。"
    "不确定就返回空数组，不补全未说出的事实。valid_from 仅在原话包含 YYYY-MM-DD 时填写，否则 null。"
    "只有当前原话明确更正或改变同一属性时，supersedes 才可引用 personal_facts 中已有条目的 id；"
    "一般新陈述不要替代旧事实，冲突保留待确认。禁止更改人物卡或基础记忆。"
    "历史与当前状态混合时，quote 只引用完整的当前子句，如‘我以前住上海，现在住杭州’中的‘现在住杭州’。"
    "personal_facts 的 counts 与 omitted 表示未全部展示，不能把省略的冲突或历史当成不存在。"
)
PREDICATES = {
    "residence": re.compile(r"住|居|搬|live|moved", re.I),
    "preferred_name": re.compile(r"叫|称呼|名字|call|name", re.I),
    "drink_preference": re.compile(r"喝|饮|咖啡|茶|drink|coffee|tea|beverage", re.I),
    "food_preference": re.compile(r"吃|食|饭|菜|eat|food", re.I),
}
CHANGE = re.compile(r"更正|纠正|改成|改为|搬到|搬去|搬家|不再|现在只|换成|以后叫|(?:现在|目前|如今)(?:住|居住|叫|喜欢|喝|吃)|correct|moved|instead|no longer", re.I)
UNSAFE = re.compile(r"[?？]|如果|假如|假设|他说|她说|朋友说|以前|去年|曾经|举例|小说|故事|引用|我听说|used to|if I|he said|she said|I heard|My (?:friend|mother|father|sister|brother)|我(?:的)?(?:朋友|同学|同事|家人)", re.I)
FUTURE = re.compile(r"明天|明年|下周|下个月|下月|将|打算|计划|准备|tomorrow|next (?:week|month|year)|plan to|will\b", re.I)
SELF = re.compile(r"^(?:(?:其实|对了)[，, ]*)?(?:我(?:现在|目前|最近|已经|已|从今天起)?|请叫我|以后叫我|更正[：:,， ]*我|I\b|My\b|Call me\b)", re.I)


def assertion_clause(text, quote):
    """Accept a complete current clause, never a substring stripped of its qualifier."""
    if not quote or text.count(quote) != 1 or any(c in text for c in '\"“”「」『』'):
        return False
    start = text.index(quote)
    # Keep question punctuation attached to its sentence for rejection.
    sentences = list(re.finditer(r"[^。！？!?;；\n]+[。！？!?;；\n]?", text))
    sentence = next((m.group() for m in sentences if m.start() <= start < m.end()), "")
    if re.search(r"[?？]|如果|假如|假设|他说|她说|朋友说|举例|小说|故事|引用|我听说|if I|he said|she said|I heard", sentence, re.I):
        return False
    clauses = [part.strip(" 。！!;；\n") for part in re.split(r"[,，]", sentence)]
    clean = quote.strip(" 。！!;；\n")
    if clean not in clauses or UNSAFE.search(clean):
        return False
    if SELF.search(clean):
        return True
    # Omitted subject is allowed only after the same speaker's explicit past clause.
    return bool(clauses and SELF.search(clauses[0]) and re.search(r"以前|去年|曾经", clauses[0])
                and not re.search(r"朋友|同学|同事|家人|他说|她说", sentence)
                and re.match(r"^(?:现在|目前|如今)(?:只)?(?:住|居住|搬到|喜欢|喝|吃|叫)", clean))


def project(rows, now):
    """An assertion supersedes named predecessors only from its effective time."""
    active = [row for row in rows if datetime.fromisoformat(row["data"]["fact"]["valid_from"]) <= now]
    retired = {key for row in active for key in row["data"]["fact"]["supersedes"]}
    values = {}
    for row in active:
        data, fact = row["data"], row["data"]["fact"]
        if data["id"] not in retired:
            values.setdefault(fact["attribute"], set()).add(fact["value"].casefold())
    result = []
    for row in rows:
        data, fact = row["data"], row["data"]["fact"]
        state = ("future" if datetime.fromisoformat(fact["valid_from"]) > now else
                 "historical" if data["id"] in retired else
                 "disputed" if len(values.get(fact["attribute"], ())) > 1 else "active")
        result.append({"id": data["id"], "version": row["version"], **fact, "status": state})
    return result


class PersonalFacts:
    def __init__(self, governed, event, clock):
        self.governed, self.event, self.clock = governed, event, clock
        self.headers = self.metadata()
        self.signature = fact_index.signature(self.headers)
        self.states = fact_index.statuses(self.headers, self.clock())
        self.available, self.rows, self.visible = True, [], set()

    def matches(self, fact):
        return (fact.get("actor"), fact.get("account"), fact.get("conversation")) == (
            self.event.actor.key, self.event.account, self.event.thread)

    def metadata(self):
        return fact_index.catalog(self.governed, self.event.actor.key, self.event.account, self.event.thread)

    def read(self, header, *, register=True):
        row = (self.governed.read("memories", header["id"]) if register else
               self.governed.contexts.get(self.governed.request, "memories", header["id"]))
        if row is None or not self.matches(row["data"].get("fact", {})) or fact_index.describe(self.governed.store, row) != header:
            raise AssistantError("context_changed", "人物事实索引与来源版本已经变化")
        return row

    def check(self):
        current = self.metadata()
        if fact_index.signature(current) != self.signature or fact_index.statuses(current, self.clock()) != self.states:
            raise AssistantError("context_changed", "人物事实在本轮期间发生变化")

    def freeze_projection(self):
        """Metadata-derived states/counts retain every source, including omitted bodies.

        One immutable policy node bounds direct inputs, not the verified DAG.
        The existing graph node/edge/depth/time limits still apply unchanged.
        """
        if not self.headers:
            return
        from .access import RecordPolicy

        with self.governed.store.lock, self.governed.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.check()
            pins = []
            for header in sorted(self.headers, key=lambda row: row["id"]):
                resource = "memories/" + header["id"]
                policy = self.governed.repository.policy(resource)
                if policy is None:
                    raise AssistantError("context_changed", "人物事实来源已删除")
                pins.append((resource, policy.version))
            identifier = stable_key("fact-view", self.event.actor.key, self.event.account, self.event.thread,
                                    self.signature, pins)
            resource = "personal-fact-view/" + identifier
            policy = RecordPolicy(resource, identifier, "personal", frozenset((self.event.actor.key,)),
                                  self.event.thread, "private", tuple(key for key, _ in pins), source_versions=tuple(pins))
            existing = self.governed.repository.policy(resource)
            if existing is None:
                self.governed.repository.set_policy(policy, None, "personal-fact-projection", db=db)
            elif existing != policy:
                raise AssistantError("context_changed", "事实视图的来源节点已经变化")
        # The file-store backend opens a separate connection for access checks.
        # Publish the metadata node first, then verify its complete pinned graph.
        self.governed.access.lease(self.governed.request, [resource])
        self.governed.resources.add(resource)

    def context(self):
        self.freeze_projection()
        ranks = {"active": 0, "disputed": 0, "future": 1, "historical": 2}
        groups = [[h for h in self.headers if h["attribute"] == attribute] for attribute in ATTRIBUTES]
        for group in groups:
            group.sort(key=lambda h: (ranks[self.states[h["id"]]], -datetime.fromisoformat(h["valid_from"]).timestamp(), h["id"]))
        # Round-robin avoids one long attribute history crowding out the other current facts.
        candidates = [group[i] for i in range(max(map(len, groups), default=0)) for group in groups if i < len(group)]
        items, self.rows = [], []
        for header in candidates[:64]:
            if len(items) >= 32:
                break
            row = self.read(header, register=False)
            item = {"id": header["id"], "version": header["version"], **row["data"]["fact"], "status": self.states[header["id"]]}
            if len(json.dumps([*items, item], ensure_ascii=False)) > 6000:
                continue
            items.append(item)
            self.rows.append(row)
            self.governed.resources.add("memories/" + header["id"])
            self.governed.versions["memories", header["id"]] = header["version"]
        self.visible = {item["id"] for item in items}
        counts = {status: list(self.states.values()).count(status) for status in ranks}
        omitted = {status: count - sum(item["status"] == status for item in items) for status, count in counts.items()}
        return {"available": True, "scope": "current_actor_account_conversation", "items": items,
                "truncated": len(items) < len(candidates), "counts": counts, "omitted": omitted,
                "history_available": True, "reason": ""}

    def supplied(self, context):
        """Only IDs actually supplied after the global budget may be replaced by the model."""
        self.visible &= {item["id"] for item in context.get("personal_facts", {}).get("items", [])}

    def save(self, updates):
        if not updates or not self.available:
            return {"written": 0, "rejected": len(updates or [])}
        store, event = self.governed.store, self.event
        prior = {row["data"]["id"]: row for row in self.rows
                 if row["data"]["id"] in getattr(self, "visible", set())}
        current = [row for row in self.headers if self.states[row["id"]] in {"active", "disputed"}]
        written = rejected = 0
        for update in updates:
            quote, value = update["quote"].strip(), update["value"].strip()
            if (not quote or not value or quote not in event.text or value not in quote
                    or not assertion_clause(event.text, quote)
                    or (FUTURE.search(quote) and update["valid_from"] is None)
                    or not PREDICATES[update["attribute"]].search(quote)
                    or (re.search(r"不|别|无|只|才|only|not|don.t", quote, re.I)
                        and not re.search(r"不|别|无|只|才|only|not|don.t", value, re.I))):
                rejected += 1
                continue
            supersedes = update["supersedes"]
            if (supersedes and not CHANGE.search(quote)) or any(
                    key not in prior or prior[key]["data"]["fact"]["attribute"] != update["attribute"]
                    for key in supersedes):
                rejected += 1
                continue
            if not supersedes and update["valid_from"] is None and any(
                    row["attribute"] == update["attribute"] and row["value_key"] == fact_index.key(store, value.casefold())
                    for row in current):
                continue
            effective = event.occurred_at
            if update["valid_from"] is not None:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", update["valid_from"]) or update["valid_from"] not in quote:
                    rejected += 1
                    continue
                try:
                    zone = ZoneInfo(store.read("policy")["data"].get("timezone", "Asia/Shanghai"))
                    effective = datetime.fromisoformat(update["valid_from"]).replace(tzinfo=zone).isoformat()
                except ValueError:
                    rejected += 1
                    continue
            try:
                date = datetime.fromisoformat(effective)
                if date.tzinfo is None:
                    raise ValueError
            except ValueError:
                rejected += 1
                continue
            # Do not make a future assertion retire an already newer fact, or permit cycles.
            if any(datetime.fromisoformat(prior[key]["data"]["fact"]["valid_from"]) > date for key in supersedes):
                rejected += 1
                continue
            identifier = stable_key("fact", event.id, update["attribute"], value, effective)
            if store.exists("memories", identifier):
                continue
            fact = {"actor": event.actor.key, "account": event.account, "conversation": event.thread,
                    "attribute": update["attribute"], "value": value, "quote": quote,
                    "valid_from": date.astimezone(timezone.utc).isoformat(), "supersedes": supersedes}
            store.write("memories", {"schema_version": 1, "id": identifier, "kind": "user_preference",
                "content": quote, "confidence": "explicit", "tags": ["personal_fact"], "locked": False,
                "fact": fact, "source": {"type": "interaction", "recorded_at": event.occurred_at,
                                         "reference": event.id}}, None, "personal-fact", identifier)
            self.governed.classify_written("memories", identifier, personal=True,
                sources=["event/" + event.id, *("memories/" + key for key in supersedes)])
            written += 1
        self.headers = self.metadata()
        self.signature = fact_index.signature(self.headers)
        self.states = fact_index.statuses(self.headers, self.clock())
        return {"written": written, "rejected": rejected}
