"""Frozen conversation batches and separate consumption/coverage receipts.

Only trusted adapters use this journal. A receipt retires input from automatic
reply scheduling; it does not claim omitted or partial text was read/replied to.
"""
from . import events
from .backlog_context import digest, group_trigger, snapshots
from .contracts import Event, stable_key
from .errors import AssistantError
from .history_status import validate_history_status


class MergedInbox:
    def __init__(self, store):
        self.store = store

    def retired(self, event, db=None):
        row = events.get(self.store, event.id)
        return (self.store.state("core-processed:" + event.id, None, db) == row["version"]
                or self.store.state("merged-consumed:" + event.id, {}, db).get("version") == row["version"])

    def record_history(self, event, status):
        """Trusted receiver report, never inferred from message text/metadata.

        Existing pending batches keep their frozen report. A later report applies
        to the next batch; the thread identity includes platform and account.
        """
        status = validate_history_status(status)
        if status is None:
            raise AssistantError("history_status_invalid", "请提供明确的历史完整性状态。")
        canonical = Event.from_dict(events.get(self.store, event.id)["event"])
        if canonical.thread != event.thread:
            raise AssistantError("history_status_scope", "历史状态不属于该会话。")
        with self.store.lock, self.store.db() as db:
            self.store.set_state("merged-history:" + canonical.thread, status, db)

    def freeze(self, incoming, *, since, generation, force_catchup=False, audience_revision=None):
        rows = snapshots([events.get(self.store, event.id) for event in incoming])
        if not rows:
            return None
        thread = rows[0]["event"]["thread"]
        with self.store.lock, self.store.db() as db:
            current = self.store.state("merged-active:" + thread, None, db)
            if current:
                record = self.store.state(current, None, db)
                if record and record["status"] == "pending":
                    outcome = self.terminal(record, db)
                    if outcome:
                        self.finish(record, outcome)
                    elif record.get("audience_revision") != audience_revision:
                        record.update(status="superseded", outcome="reply_scope_changed")
                        self.store.set_state(current,record,db)
                    else:
                        try:
                            self.check(record)
                        except AssistantError as exc:
                            if exc.code not in {"context_changed", "unknown_evidence"}:
                                raise
                            record.update(status="superseded", outcome="source_changed_before_send")
                            self.store.set_state(current, record, db)
                        else:
                            if record["generation"] != generation:
                                record.update(catchup=True, generation=generation)
                                self.store.set_state(current, record, db)
                            return record
            rows = [row for row in rows if not self.retired(Event.from_dict(row["event"]), db)]
            if not rows:
                return None
            anchor = next((row for row in reversed(rows) if group_trigger(row)), rows[-1])
            pins = [[row["id"], row["version"]] for row in rows]
            identifier = digest([audience_revision,pins]) if audience_revision else digest(pins)
            key = "merged-batch:" + identifier
            record = {"id": identifier, "key": key, "thread": thread, "anchor": anchor["id"],
                      "history_status": validate_history_status(self.store.state("merged-history:" + thread, None, db)),
                      "members": pins, "status": "pending", "coverage": None, "generation": generation,
                      "platform": anchor["event"]["platform"], "account": anchor["event"]["account"],
                      "catchup": force_catchup or any(events.utc_time(row["event"]["occurred_at"]) < events.utc_time(since)
                                     for row in rows)}
            if audience_revision:
                record["audience_revision"] = audience_revision
            self.store.set_state(key, record, db)
            self.store.set_state("merged-active:" + thread, key, db)
            self.store.set_state("merged-anchor:" + anchor["id"], key, db)
            return record

    def terminal(self, record, db):
        """Recover a terminal send without generating or submitting it again."""
        for capability in ("message.reply", "conversation.end"):
            key = stable_key("dispatch", record["platform"], record["account"], record["anchor"], capability)
            row = db.execute("SELECT status FROM core_actions WHERE dedupe=?", (key,)).fetchone()
            if row and row["status"] in {"succeeded", "confirmed", "uncertain", "dispatching"}:
                return "uncertain" if row["status"] == "dispatching" else row["status"]
        if self.store.state("core-processed:" + record["anchor"], None, db) == dict(record["members"])[record["anchor"]]:
            return "recovered_terminal_receipt"
        return None

    def for_event(self, event):
        key = self.store.state("merged-anchor:" + event.id)
        record = self.store.state(key) if key else None
        return record if record and record["status"] == "pending" else None

    def check(self, record):
        current = self.store.state(record["key"])
        if (not current or current["status"] != "pending" or current["members"] != record["members"]
                or current.get("history_status") != record.get("history_status")):
            raise AssistantError("context_changed", "合并批次已经变化。")
        result = []
        for identifier, version in record["members"]:
            source = events.get(self.store, identifier)
            if source["version"] != version or source["event"]["thread"] != record["thread"]:
                raise AssistantError("context_changed", "合并批次的原始消息已经变化。")
            result.append(Event.from_dict(source["event"]))
        return result

    def prepared(self, record, report):
        with self.store.lock, self.store.db() as db:
            self.check(record)
            current = self.store.state(record["key"], db=db)
            current["coverage"] = {key: report[key] for key in
                                   ("coverage", "sources", "complete_sources", "source_spans", "selection")}
            current["coverage"]["history_status"] = validate_history_status(report.get("history_status"))
            current["coverage"]["media_budget"] = report.get("media_budget")
            current["coverage"]["attachment_text"] = report.get("attachment_text")
            current["coverage"]["image_sources"] = report.get("image_sources", [])
            self.store.set_state(record["key"], current, db)

    def finish(self, record, outcome):
        """Called after the core's terminal receipt, or a no-trigger decision."""
        with self.store.lock, self.store.db() as db:
            current = self.store.state(record["key"], db=db)
            if current["status"] == "completed":
                return current
            coverage = current.get("coverage") or {}
            included = dict(coverage.get("sources", ()))
            complete = dict(coverage.get("complete_sources", ()))
            for identifier, version in current["members"]:
                exposure = ("complete" if complete.get(identifier) == version else
                            "partial" if included.get(identifier) == version else "omitted")
                self.store.set_state("merged-consumed:" + identifier,
                                     {"version": version, "batch": current["id"],
                                      "coverage": exposure, "outcome": outcome}, db)
            current.update(status="completed", outcome=outcome)
            self.store.set_state(record["key"], current, db)
            return current
