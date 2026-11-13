"""Resolve a stated WeChat ID against the live source before certifying a pending pair."""
import time

from companion_core import events
from companion_core.contracts import ActorRef, Event, stable_key
from companion_core.decision import IDENTITY_INSTRUCTIONS
from companion_core.errors import AssistantError
from companion_core.manual_identity import inspect_pair
from companion_core.private_identity_evidence import prepare
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.schema import IDENTITY_PROPOSALS, obj


class AliasReviews:
    def __init__(self):
        self.after = ""
        self.next_poll = 0

    async def poll(self, core, receiver):
        if receiver.status()["state"] != "connected" or time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + 15
        try:
            await self._review_next(core, receiver)
        except AssistantError as exc:
            if exc.code not in {"context_changed", "access_denied", "unknown_evidence", "identity_changed",
                                "identity_conflict", "identity_already_linked", "identity_locked", "unknown_actor"}:
                raise

    async def _review_next(self, core, receiver):
        graph = core.identities
        with core.store.db() as db:
            pending = db.execute("SELECT 1 FROM core_identity_links WHERE status='suspected' LIMIT 1").fetchone()
        if not pending or core.remaining_model_calls() < 2:
            return
        with core.store.lock:
            snapshot = graph._snapshot()
        records = [r for r in snapshot["links"] if r["effective_status"] == "suspected"
                   and not r["data"]["locked"] and not r["data"]["manual"]
                   and r["data"]["right"]["platform"] == "wechat"
                   and r["data"]["right"]["scope"] in ("", receiver.account)
                   and not core.store.state(reviewed_key(r))]
        hints = sorted({r["data"]["right"]["user_id"] for r in records})
        if not hints:
            return
        hint = next((value for value in hints if value > self.after), hints[0])
        self.after = hint
        username = await receiver.resolve_alias(hint)
        if not username:
            return
        actor = ActorRef("wechat", username, receiver.account)
        records = [r for r in records if r["data"]["right"]["user_id"] == hint
                   and ActorRef(**r["data"]["right"]) != actor]
        if not records or len(records) > 5:
            return
        scope = ReplyScope("wechat", receiver.bindings.identity, "private", username)
        permissions = ReplyAuthorizations(core.store)
        permission = permissions.record(scope)
        if not permission["enabled"]:
            return
        incoming = [Event.from_dict(row["event"]) for row in events.recent(core.store, actor=actor, limit=12)
                    if row["event"]["visibility"] == "private"]
        if not incoming:
            return
        governed = core.governed_context(incoming[-1])
        request = governed.authority.for_event(incoming[-1], purpose="maintenance")
        # The substituted endpoint exists only for this call until the model supplies both proofs.
        scoped = [{**r, "claimed_alias": hint, "data": {**r["data"], "right": actor.as_dict()}} for r in records]
        evidence = prepare(graph, {**snapshot, "links": scoped}, incoming, governed.access, request)
        if evidence is None:
            return
        record = evidence.record
        doc = record["data"]
        left = ActorRef(**doc["left"])
        try:
            preview, _, link_id, direct = inspect_pair(core.store, left.key, actor.key)
        except AssistantError as exc:
            if exc.code in {"identity_already_linked", "identity_locked", "identity_conflict", "unknown_actor"}:
                return
            raise
        attempt = stable_key("alias-review-attempt", record["version"], actor.key,
                             [(p["event"]["id"], p["version"]) for p in evidence.evidence])
        if core.store.state(attempt):
            return
        generation = core.guard()["generation"]

        def guard():
            core.guard(generation)
            evidence.check(graph, governed.access, request)
            if permissions.record(scope) != permission or receiver.status()["state"] != "connected":
                raise AssistantError("context_changed", "微信连接或会话授权已变化")

        guard()
        context = {"candidates": [{k: doc[k] for k in ("left", "right", "status")}],
                   "identity_evidence": evidence.evidence}
        answer = await core.model("仅复核当前两个账号。微信号与收件主体已经在线核对。"+IDENTITY_INSTRUCTIONS,
                                  context, obj({"identity_proposals": IDENTITY_PROPOSALS}), guard=guard)
        guard()
        if await receiver.resolve_alias(hint) != username:
            return
        with core.store.lock, core.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            guard()
            if core.store.state(attempt, db=db):
                return
            latest, _, _, _ = inspect_pair(core.store, left.key, actor.key)
            if latest != preview:
                return
            decision = next((p for p in answer["identity_proposals"] if valid_decision(p, doc, evidence.evidence)), None)
            if decision:
                if direct is None:
                    seed = {**doc, "id": link_id, "history": [*doc["history"], {
                        "kind": "verified_wechat_alias", "status": "suspected", "source": doc["id"],
                        "source_version": record["version"]}][-100:]}
                    core.store.write("identity_links", seed, None, "identity-core", link_id)
                graph.apply(decision, allowed_evidence={item["event"]["id"] for item in evidence.evidence})
                core.store.set_state(reviewed_key(record), True, db)
            core.store.set_state(attempt, True, db)


def reviewed_key(record):
    return stable_key("resolved-wechat-claim", record["data"]["id"], record["version"])


def valid_decision(proposal, candidate, evidence):
    if (proposal["kind"] != "correspondence" or not proposal["specific_correspondence"]
            or proposal["left"] != candidate["left"] or proposal["right"] != candidate["right"]):
        return False
    refs = proposal["evidence"]
    seen = {item["event"]["id"]: item["event"]["actor"] for item in evidence}
    return (len({ref["event_id"] for ref in refs}) == len(refs)
            and all(len(ref["quote"].strip()) >= 8 and any(
                ref["event_id"] == item["event"]["id"] and ref["version"] == item["version"]
                and ref["quote"] in item["event"]["text"] for item in evidence) for ref in refs)
            and {ActorRef(**seen[ref["event_id"]]).key for ref in refs} ==
                {ActorRef(**candidate[side]).key for side in ("left", "right")})
