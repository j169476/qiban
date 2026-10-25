"""Shared durable execution protocol; platform rules are injected hooks."""
import asyncio
from weakref import WeakValueDictionary

from .errors import AssistantError

_ACTION_LOCKS = WeakValueDictionary()


def action_lock(store, action_id):
    # Async locks protect tasks sharing a thread; SQLite CAS remains authoritative
    # across processes and Store instances. Weak values release idle action locks.
    key = (asyncio.get_running_loop(), str(store.root), action_id)
    lock = _ACTION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ACTION_LOCKS[key] = lock
    return lock


class ActionExecution:
    def record_execution_policy(self, run, policy_version, db):
        """Persist execution metadata through the controller's run authority."""
        run["policy_version"] = policy_version
        self.store.set_state("run", run, db)

    def execution_stopped(self, run):
        return (self.store.runtime / f"stop-{run['generation']}").exists()

    async def execute(self, action_id, *, guard=None):
        async with action_lock(self.store, action_id):
            return await self._execute(action_id, guard=guard)

    async def _execute(self, action_id, *, guard=None):
        run = self.role_run()
        if guard:
            guard()
        if self.adapter is None:
            raise AssistantError("adapter_missing", "未连接平台适配器")
        with self.store.lock:
            with self.store.db() as db:
                row = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
                if not row:
                    raise AssistantError("not_found", "未找到动作")
                action = dict(row)
            if action["owner"] != self.identity() or action["generation"] != run["generation"]:
                raise AssistantError("stale_action", "动作不属于当前角色运行")
            if action["status"] != "prepared":
                return {"id": action_id, "status": action["status"], "sent": False}
            payload = self._payload(action["kind"], self.store.ledger_decode("actions.payload", action["payload"]))
            await self.action_context(payload)
            # Verify account before reservation; never log returned credentials.
            me = await self.adapter.me()
            if str(me["uid"]) != run["account"]:
                raise AssistantError("account_mismatch", "登录账号与绑定 UID 不一致")
            policy = self.store.read("policy")
            self.run(run["generation"])
            if guard:
                guard()
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone()
                if current["status"] != "prepared":
                    return {"id": action_id, "status": current["status"], "sent": False}
                day, target_uid = self._eligibility(db, run, action, payload, policy["data"])
                # Re-query the real owner immediately before dispatch, even inside a scheduled worker.
                if self.store.read("policy")["version"] != policy["version"]:
                    raise AssistantError("policy_changed", "发送前策略发生变化，请重新评估")
                updated = db.execute(
                    "UPDATE actions SET status='dispatching',day=?,target_uid=?,dispatched=? "
                    "WHERE id=? AND status='prepared'",
                    (day, target_uid, self.clock().isoformat(), action_id),
                )
                if updated.rowcount != 1:
                    raise AssistantError("action_claim_lost", "该动作已由另一执行者接管")
                self.record_execution_policy(run, policy["version"], db)
            # Check stop marker after the reservation commit as well.
            if self.execution_stopped(run):
                result = AssistantError("before_send_cancelled", "预留提交后运行已暂停，确定尚未派发").as_dict()
                with self.store.db() as db:
                    db.execute("UPDATE actions SET status='cancelled',result=? WHERE id=?",
                               (self.store.ledger_encode("actions.result", result), action_id))
                return {"id": action_id, "status": "cancelled", "sent": False, "result": result}
            try:

                def before_send():
                    try:
                        self.run(run["generation"])
                        if guard:
                            guard()
                        if self.store.read("policy")["version"] != policy["version"]:
                            raise AssistantError("policy_changed", "策略已改变")
                        if self.store.read("manifest")["data"]["account_uid"] != run["account"]:
                            raise AssistantError("account_changed", "账号已改变")
                    except AssistantError:
                        raise AssistantError("before_send_cancelled", "实际派发前运行或策略已变化") from None

                result = await self.adapter.perform(action["kind"], payload, policy["data"], before_send)
                state = "succeeded"
            except AssistantError as exc:
                # Only explicit platform rejection proves no side effect occurred.
                state = (
                    "cancelled"
                    if exc.code == "before_send_cancelled"
                    else "failed"
                    if exc.code == "platform_rejected"
                    else "uncertain"
                )
                result = exc.as_dict()
            except Exception:
                state, result = "uncertain", {"error": "transport_or_unknown"}
            with self.store.db() as db:
                spent = (
                    result.get("units_spent", action["units"]) if state == "succeeded" else action["units"]
                )
                if type(spent) is not int or not 0 <= spent <= action["units"]:
                    spent = action["units"]
                db.execute(
                    "UPDATE actions SET status=?,result=?,units=? WHERE id=?",
                    (state, self.store.ledger_encode("actions.result", result), spent, action_id),
                )
            self.store.audit("actions", {"id": action_id, "kind": action["kind"], "status": state})
            with self.store.db() as db:
                recorded = dict(db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone())
            self.record_action(recorded)
            return {"id": action_id, "status": state, "result": result, "sent": state != "cancelled"}
