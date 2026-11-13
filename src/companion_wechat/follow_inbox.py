"""Per-conversation receive positions for a confirmed account-wide reply grant."""

import hashlib
from bisect import bisect_right

from companion_core.contracts import stable_key
from companion_core.history_status import validate_history_status
from companion_core.reply_audience import read

from .contracts import require
from .db_source import validate_checkpoint
from .receive_window import complete, validate_window


def event_scope_key(account, observation):
    return stable_key("wechat-follow-event", account, observation)


class FollowInbox:
    def __init__(self, receiver):
        self.receiver = receiver
        self.last = ""

    def key(self, target, revision):
        return stable_key("wechat-follow-position", self.receiver.account, target, revision)

    def position(self, target):
        grant = self.receiver.follow_directory.chats.get(target)
        if grant:
            return self.receiver.inbox.checkpoint(self.key(target, grant[1]))

    async def pump(self):
        receiver = self.receiver
        chats = receiver.follow_directory.chats
        if not chats:
            return
        names = sorted(chats)
        start = bisect_right(names, self.last) % len(names)
        for i in range(min(8, len(names))):
            name = names[(start + i) % len(names)]
            await self.read_one(name, *chats[name])
            self.last = name

    async def read_one(self, target, category, revision, since):
        r = self.receiver
        members = r.follow_directory.live[category]
        tenure = members.token(revision, target)
        if tenure is None:
            return
        key = self.key(target, revision)
        before = r.inbox.checkpoint(key)
        new_tenure = not before or before["tenure"] != tenure
        resume = bool(before and not new_tenure and before.get("window"))
        catchup = new_tenure or (resume and before["catchup"])
        worker = r._worker
        data = await worker.request(
            "member_window",
            username=target,
            kind=category.kind,
            since=since,
            checkpoint=before["checkpoint"] if before else None,
            window=before["window"] if resume else None,
            limit=min(100, r.capacity),
        )
        require(isinstance(data, dict) and type(data.get("present")) is bool, "wechat_source_invalid")
        if not data["present"]:
            members.remove(target)
            return
        alias = "target_" + hashlib.sha256(target.encode()).hexdigest()[:32]
        spec = {"username": target, "kind": category.kind}
        enrollment = {
            **r._saved["enrollment"],
            "schema_version": 2,
            "targets": {alias: spec},
            "target_since": {alias: since},
        }
        enrollment.pop("display_names", None)
        checkpoint = validate_checkpoint(data["next_checkpoint"], enrollment)
        window = validate_window(data["window"], {"enrollment": enrollment, "checkpoint": checkpoint})
        if resume:
            require(
                all(window["sources"].get(k) == v for k, v in before["window"]["sources"].items()),
                "wechat_source_regressed",
            )
        done = complete({"checkpoint": checkpoint, "window": window})
        require(
            type(data["window_complete"]) is bool and done == data["window_complete"], "wechat_source_invalid"
        )
        for k, item in (before["checkpoint"]["sources"] if before else {}).items():
            current = checkpoint["sources"].get(k)
            require(
                current
                and current["salt"] == item["salt"]
                and (current["anchor"] or {}).get("local_id", 0) >= (item["anchor"] or {}).get("local_id", 0),
                "wechat_source_regressed",
            )
            if item["anchor"] and current["anchor"]["local_id"] == item["anchor"]["local_id"]:
                require(current["anchor"] == item["anchor"], "wechat_source_regressed")
        observations = r._observations(data["rows"], enrollment)
        history = validate_history_status(data["history_status"][alias])
        require(history is not None, "wechat_source_invalid")
        if before and before.get("window") and before.get("history", {}).get("status") == "incomplete":
            history = {
                "status": "incomplete",
                "reasons": list(dict.fromkeys(before["history"]["reasons"] + history["reasons"])),
            }
        after = {
            "tenure": tenure,
            "checkpoint": checkpoint,
            "window": None if done else window,
            "catchup": bool(catchup),
            "history": history,
        }
        with r.store.lock, r.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                r._closed
                or r._worker is not worker
                or members.token(revision, target) != tenure
                or read(r.store, category, db)["version"] != revision
            ):
                return
            if observations:
                r.ensure_follow_binding(target, category.kind, alias, db)
            for item in observations:
                r.store.set_state(
                    event_scope_key(r.account, item.observation_id),
                    {"revision": revision, "catchup": bool(catchup), "history": history},
                    db,
                )
            if after != before or observations:
                count = r.inbox.publish_batch(
                    observations, checkpoint_key=key, expected_checkpoint=before, checkpoint=after, db=db
                )
                r._queued += count
