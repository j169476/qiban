"""Immutable, conversation-scoped private growth using the normal reply model call."""
from copy import deepcopy
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from . import emotions
from .contracts import stable_key
from .errors import AssistantError
from .schema import EMOTION_CHANGE, obj


def text(length, minimum=0):
    return {"type": "string", "maxLength": length, "minLength": minimum}


REFLECTION = obj({"content": text(2000, 1), "traits": {"type": "array", "maxItems": 6, "items": text(300)},
    "speech_habits": {"type": "array", "maxItems": 6, "items": text(300)},
    "opinions": {"type": "array", "maxItems": 6, "items": text(300)}})
UPDATE = obj({"emotion": {"anyOf": [{"type": "null"}, EMOTION_CHANGE]},
    "interests": {"type": "array", "maxItems": 3, "items": obj({"topic": text(100, 1),
        "delta": {"type": "number", "minimum": -0.05, "maximum": 0.05}, "quote": text(1000, 1)})},
    "reflection": {"anyOf": [{"type": "null"}, REFLECTION]}})
EMOTION_STATE = obj({"label": text(100), "reason": text(1000), "valence": {"type": "number", "minimum": -1, "maximum": 1},
    "arousal": {"type": "number", "minimum": 0, "maximum": 1}, "updated_at": {"type": "string", "format": "date-time"},
    "account_uid": text(200), "event_id": text(200), "day": {"type": "string", "format": "date"},
    "change_today": {"type": "number", "minimum": 0, "maximum": 20}})
STATE = obj({"interests": {"type": "object", "maxProperties": 100, "propertyNames": text(100, 1),
    "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1}},
    **{key: {"type": "array", "maxItems": 6, "items": text(300)} for key in ("traits", "speech_habits", "opinions")},
    "pending_experiences": {"type": "array", "maxItems": 12, "items": obj({"event_id": text(200),
        "content": text(1200), "excerpt": {"type": "boolean"}})},
    "interest_change": {"type": "number", "minimum": 0, "maximum": .300001},
    "interest_day": {"type": "string", "format": "date"}})
STATE["properties"].update(emotion=EMOTION_STATE, reflection=text(2000), reflection_day={"type": "string", "format": "date"})
SCHEMA = obj({"schema_version": {"const": 1}, "id": text(80, 1), "account": text(200, 1),
    "actor": text(600, 1), "conversation": text(1000, 1), "channel": text(80, 1),
    "state": STATE, "source": obj({"type": {"const": "interaction"},
        "recorded_at": text(100, 1), "reference": text(200, 1)}), "locked": {"const": False}})
PROMPT = (
    " private_growth 仅为当前私密会话中的成长，不代表公开状态。growth_update 可为 null。"
    "情绪必须引用 growth_evidence 中当前真实事件；兴趣只根据当前消息的逐字 quote 小幅变化，"
    "区分用户兴趣与你自己的感受，反讽、角色扮演、转述不能当作稳定偏好。"
    "仅 private_growth.reflection_due 为 true 时才可生成 reflection，依据本轮实际提供的"
    "待反思经历、对话和记忆。反思只是动态成长，不修改人物卡与基础记忆。"
    "没有依据则对应更新为 null 或空数组；不生成新的权限、身份或工具指令。"
)


class InteractionGrowth:
    def __init__(self, core, governed, event):
        self.core, self.store, self.governed, self.event = core, core.store, governed, event
        self.key = stable_key("private-growth", event.account, event.actor.key, event.thread)
        self.previous_id = self.store.state(self.key)
        previous = governed.read("interaction_growth", self.previous_id) if self.previous_id else None
        if previous and any(previous["data"][key] != value for key, value in (
                ("account", event.account), ("actor", event.actor.key), ("conversation", event.thread),
                ("channel", event.platform))):
            raise AssistantError("growth_scope", "私密成长记录归属不匹配")
        self.state = deepcopy(previous["data"]["state"]) if previous else {
            "interests": {}, "traits": [], "speech_habits": [], "opinions": [], "pending_experiences": []}
        self.settings = self.store.read("companion")["data"]
        self.now = core.clock()
        self.day = self.now.astimezone(ZoneInfo(self.store.read("policy")["data"]["timezone"])).date().isoformat()
        parent = self

        class View:
            def read(self, kind):
                if kind == "growth":
                    return {"data": parent.state}
                if kind == "manifest":
                    return {"data": {"account_uid": event.account}}
                return parent.store.read(kind)

        self.emotion = emotions.current(SimpleNamespace(store=View(), clock=core.clock))
        self.due = (self.settings.get("daily_reflection", True)
            and self.now.astimezone(ZoneInfo(self.store.read("policy")["data"]["timezone"])).hour
                >= self.settings.get("reflection_hour", 21)
            and self.state.get("reflection_day") != self.day
            and len(self.state.get("pending_experiences", [])) >= self.settings.get("reflection_min_memories", 3))

    def context(self):
        return {"current_emotion": self.emotion, "private_growth": {**self.state, "reflection_due": self.due},
                "growth_evidence": [self.event.id]}

    def check(self):
        if self.store.state(self.key) != self.previous_id:
            raise AssistantError("context_changed", "私密成长已被另一条互动更新")

    def save(self, update, memories, event_version):
        self.check()
        identifier = stable_key("growth", self.event.id, event_version)
        # A revoked, edited or deleted record is never silently regenerated.
        if self.store.state("growth-recorded:" + identifier):
            return
        state = deepcopy(self.state)
        update = update or {"emotion": None, "interests": [], "reflection": None}
        emotion = update["emotion"]
        if emotion and emotion["evidence_ids"] != [self.event.id]:
            raise AssistantError("ungrounded_emotion", "私聊情绪需要本轮真实事件依据")
        if emotion and self.settings.get("emotion_enabled", True):
            state["emotion"] = emotions.evolve(self.emotion, emotion, self.settings, self.now, identifier)
        topics = [item["topic"] for item in update["interests"]]
        if len(topics) != len(set(topics)) or any(item["quote"] not in self.event.text for item in update["interests"]):
            raise AssistantError("ungrounded_growth", "私聊兴趣需要不重复的主题及当前消息原文")
        changed = state.get("interest_change", 0) if state.get("interest_day") == self.day else 0
        for item in update["interests"]:
            topic = item["topic"]
            if topic not in state["interests"] and len(state["interests"]) >= 100:
                continue
            delta = max(-max(0, .3 - changed), min(max(0, .3 - changed), item["delta"]))
            state["interests"][topic] = round(max(0, min(1, state["interests"].get(topic, .3) + delta)), 6)
            changed += abs(delta)
        state.update(interest_day=self.day, interest_change=changed)
        reflection = update["reflection"]
        if reflection:
            if not self.due:
                raise AssistantError("ungrounded_growth", "尚未达到当前私聊的反思条件")
            state.update({key: reflection[key] for key in ("traits", "speech_habits", "opinions")})
            state.update(reflection=reflection["content"], reflection_day=self.day, pending_experiences=[])
        pending = state.get("pending_experiences", [])
        pending.extend({"event_id": self.event.id, "content": value[:1200], "excerpt": len(value) > 1200}
                       for value in memories if value.strip())
        state["pending_experiences"] = pending[-12:]
        self.store.write("interaction_growth", {"schema_version": 1, "id": identifier,
            "account": self.event.account, "actor": self.event.actor.key, "conversation": self.event.thread,
            "channel": self.event.platform, "state": state, "locked": False,
            "source": {"type": "interaction", "recorded_at": self.now.isoformat(), "reference": self.event.id}},
            None, "private-interaction-growth", identifier)
        self.governed.classify_written("interaction_growth", identifier)
        self.store.set_state(self.key, identifier)
        self.store.set_state("growth-recorded:" + identifier, True)
        self.previous_id = identifier
