"""Source-account relationship updates, daily bounds and durable idempotency."""
from zoneinfo import ZoneInfo

from .errors import AssistantError


class RelationshipUpdates:
    def relationship(self, uid, display_name, delta, event_id, impression="", facts=None):
        self.mode("role")
        if isinstance(delta, bool) or not isinstance(delta, int) or abs(delta) > 3:
            raise AssistantError("affinity_delta", "单次好感变化必须为 -3 至 3 的整数")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
            raise AssistantError("invalid_event", "需要非空、稳定的经历 ID")
        with self.store.lock:
            from .relationship_store import commit, pending, receipt, recover

            recover(self.store, uid)
            if pending(self.store, uid):
                raise AssistantError("relationship_recovery_conflict", "该 UID 有待裁决提交，请在管理入口检查")
            try:
                old = self.store.read("relationships", uid)
            except AssistantError as exc:
                if exc.code != "not_found":
                    raise
                old = {
                    "version": None,
                    "data": {
                        "schema_version": 1,
                        "id": uid,
                        "display_name": display_name,
                        "affinity": 50,
                        "interactions": 0,
                        "events": [],
                        "locked": False,
                    },
                }
            doc = old["data"]
            # Persistent dedupe even after the editable recent-events list is compacted.
            seen_key = receipt(uid, event_id)
            if self.store.state(seen_key) or event_id in doc["events"]:
                if not self.store.state(seen_key):
                    self.store.set_state(seen_key, True)
                return {"duplicate": True, **old}
            day = self.clock().astimezone(ZoneInfo(self.store.read("policy")["data"]["timezone"])).date()
            spent = doc.get("affinity_change_today", 0) if doc.get("affinity_day") == day.isoformat() else 0
            remaining = max(0, self.store.read("companion")["data"]["affinity_daily_change"] - spent)
            applied = min(abs(delta), remaining) * (1 if delta >= 0 else -1)
            doc.update(
                display_name=display_name or doc["display_name"],
                affinity=max(0, min(100, doc["affinity"] + applied)),
                interactions=doc["interactions"] + 1,
                events=(doc["events"] + [event_id])[-100:],
                last_interaction_at=self.clock().isoformat(),
                affinity_day=day.isoformat(),
                affinity_change_today=spent + abs(applied),
            )
            if impression.strip():
                doc["impression"] = impression[:2000]
            if facts:
                doc["facts"] = list(dict.fromkeys(doc.get("facts", []) + facts))[-20:]
            result = commit(self.store, doc, old["version"], event_id, self.identity())
            from .persona import annotate_relationship

            return {**annotate_relationship({"kind": "relationships", **result},
                                           self.store.read("persona")["data"]), "applied_delta": applied}
