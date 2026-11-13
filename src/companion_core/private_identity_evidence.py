"""Narrow evidence for an existing identity claim, private to one review call."""
import re
from dataclasses import dataclass

from . import events
from .contracts import ActorRef, Event
from .errors import AssistantError

# A long run without a natural boundary is unsuitable for sharing as a quote.
BOUNDARY = re.compile(r"(?<=[。！？!?，,；;：:])|(?<=\.)(?=\s)|[\n\r]+")
PLAN = re.compile(r"约|约定|见面|碰面|说好|商量|讨论|暗号|口令|安排|计划|约好|agreed|meet|plan|codeword", re.I)
SELF = re.compile(r"我(?:的|是|在|用)|本人|my\b|I(?:'m| am| use)\b", re.I)


def clauses(text):
    return [part for raw in BOUNDARY.split(text) if 0 < len(part := raw.strip()) <= 180]


def identifier(text, value):
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(value) + r"(?![A-Za-z0-9_])", text) is not None


def matching_detail(anchor, reply):
    # Retrieval only. The independent reviewer still has to establish correspondence.
    filler = r"我们|我俩|今天|上次|一起|约在|约定|说好|到时候|之前|[^\w]"
    a = re.sub(filler, "", anchor.casefold())
    b = re.sub(filler, "", reply.casefold())
    return any(a[i:i + 5] in b for i in range(len(a) - 4))


def snippets(claim, message, response, claimant, counterpart_id):
    original, incoming = clauses(message), clauses(response)
    index = next((i for i,s in enumerate(original)
                  if identifier(claim, counterpart_id) and identifier(s, counterpart_id)
                  and SELF.search(s) and (s.rstrip("。！？!?，,；;：:") in claim or claim in s)), None)
    if index is None:
        return None
    statement = original[index]
    nearby = original[max(0, index - 1):index + 2]
    for reply in incoming:
        if SELF.search(reply) and identifier(reply, claimant.user_id):
            return [statement], [reply]
        for detail in nearby:
            if PLAN.search(detail) and matching_detail(detail, reply):
                return list(dict.fromkeys([statement, detail])), [reply]
    return None


def eligible_policy(access, item):
    event = item["event"]
    actor = ActorRef(**event["actor"])
    policy = access.repository.policy("event/" + event["id"])
    return (event["visibility"] == "private" and not actor.scope.startswith("wechat-provisional-")
            and event.get("metadata", {}).get(actor.platform, {}).get("identity_quality", "verified") == "verified"
            and policy is not None and policy.valid and policy.classification == "personal"
            and policy.owners == {actor.key} and policy.audience == "private" and policy.conversation == event["thread"]
            and policy.version == item["version"] and not policy.sources and not policy.source_versions
            and (policy.expires_at is None or policy.expires_at > access.authority.clock()))


def excerpt(item, text):
    event = item["event"]
    return {"event": {**{k: event[k] for k in ("id", "platform", "actor", "occurred_at", "visibility")}, "text": text},
            "version": item["version"], "excerpt": True}


@dataclass
class PrivateReview:
    record: dict
    evidence: list

    def check(self, graph, access, request):
        access.authority.require(request)
        if graph.store.read("identity_links", self.record["data"]["id"])["version"] != self.record["version"]:
            raise AssistantError("context_changed", "候选身份已变化")
        for event_id, version in {(e["event"]["id"], e["version"]) for e in self.evidence}:
            current = events.get(graph.store, event_id)
            if current["version"] != version or not eligible_policy(access, current):
                raise AssistantError("context_changed", "身份依据已变化，请重新核对")


def prepare(graph, snapshot, triggers, access, request):
    if not triggers or request.purpose != "maintenance" or request.audience != "private":
        return None
    incoming = []
    remaining = graph.REVIEW_TEXT_LIMIT
    for item in reversed(triggers):
        if item.actor.key != request.actor or item.thread != request.conversation:
            continue
        cost = len(item.text) + 128
        if cost > remaining:
            continue
        remaining -= cost
        incoming.append(item)
    if not incoming:
        return None
    actor = incoming[0].actor
    found = []
    for record in graph._candidates(actor, snapshot["links"])[:5]:
        doc = record["data"]
        # Only an original first-person claim naming this specific incoming account.
        people = [ActorRef(**doc[side]) for side in ("left", "right")]
        claimant = next(a for a in people if a != actor)
        for ref in doc["evidence"]:
            source = snapshot["events"].get(ref["event_id"])
            if (not source or source["deleted"] or source["version"] != ref["version"]
                    or source["event"]["actor"] != claimant.as_dict() or not eligible_policy(access, source)):
                continue
            for item in incoming:
                selected = snippets(ref["quote"], source["event"]["text"], item.text, claimant, record.get("claimed_alias", actor.user_id))
                if selected:
                    current = events.get(graph.store, item.id)
                    if Event.from_dict(current["event"]) != item or not eligible_policy(access, current):
                        continue
                    parts = [excerpt(source, s) for s in selected[0]] + [excerpt(current, s) for s in selected[1]]
                    found.append(PrivateReview(record, parts))
                    break
            if found and found[-1].record is record:
                break
        if len(found) > 1:
            return None
    return found[0] if found else None
