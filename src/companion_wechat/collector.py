"""Standalone receive-only ingestion into an already initialized encrypted store.

No role runtime, model, memory retrieval, sending, migration, or SQLite fallback
is constructed here. The source gateway owns its separately supervised worker.
"""
from __future__ import annotations

import asyncio
import math
import time

from filelock import FileLock, Timeout

from companion_core import events
from companion_core.contracts import Event, stable_key
from companion_core.errors import AssistantError
from companion_core.runtime_keys import RUN_KEYS

from .contracts import identifier, require
from .receiver_transport import ERROR_CODES

_ERRORS = {
    "collector_failed", "collector_configuration_invalid", "collector_already_running",
    "collector_role_conflict", "collector_store_required", "collector_schema_required",
    "collector_key_invalid", "collector_scope_invalid", "collector_cursor_conflict",
    "collector_source_writable", "collector_event_conflict", "collector_cleanup_failed",
    "collector_service_dependency_missing",
    "service_missing", "service_config", "service_vault", "database_runtime",
    "database_timeout", "database_unavailable", "database_error", "storage_integrity",
    "wechat_disconnected", "wechat_account_mismatch", "wechat_account_rebind",
    "wechat_receive_failed", "wechat_stale_connection", "wechat_target_unbound",
    "wechat_target_disabled", "wechat_binding_conflict", "wechat_inbox_full",
    "wechat_checkpoint_conflict", "wechat_observation_changed", "wechat_observation_conflict",
    "wechat_event_conflict", "wechat_cursor_invalid", "wechat_cursor_stream",
    "wechat_cursor_regressed", "configuration_invalid", "client_not_running",
    "client_ambiguous", "client_version_unsupported", "client_identity_changed",
    "process_access_denied", "key_not_found", "account_missing_or_ambiguous",
    "target_missing_or_ambiguous", "database_generation_changed", "dependency_missing",
    "dependency_version_unsupported", "worker_failed", "worker_timeout",
    "wechat_receiver_state", "wechat_receiver_owner_conflict", "wechat_receiver_failed",
    "wechat_source_invalid", "wechat_source_regressed", "wechat_source_generation_changed",
}
_ERRORS |= set(ERROR_CODES)
_STATES = {"starting", "ready", "collecting", "complete", "interrupted"} | _ERRORS
_SOURCE_STATES = {"new", "connected", "blocked", "retrying", "waiting_for_source", "backpressure", "closed"}


def source_report(value):
    result = {"state": "new", "code": "", "reconnects": 0, "queued": 0, "skipped": 0,
              "blocked_rows": 0, "worker_connected": False, "retry_in_seconds": 0.0}
    if not isinstance(value, dict):
        return result
    state, code = value.get("state"), value.get("code", "")
    result["state"] = state if type(state) is str and state in _SOURCE_STATES else "new"
    result["code"] = code if type(code) is str and code in _ERRORS | {"", "source_rows_pending"} else "collector_failed"
    for key in ("reconnects", "queued", "skipped", "blocked_rows"):
        number = value.get(key, 0)
        if type(number) is int and 0 <= number <= 2**63 - 1:
            result[key] = number
    result["worker_connected"] = value.get("worker_connected") is True
    delay = value.get("retry_in_seconds", 0.0)
    if type(delay) in (int, float) and math.isfinite(delay) and 0 <= delay <= 300:
        result["retry_in_seconds"] = round(delay, 3)
    return result


def validate_options(once, duration_seconds, poll_seconds):
    require(type(once) is bool and (duration_seconds is None or
            type(duration_seconds) is int and 1 <= duration_seconds <= 2**31)
            and not (once and duration_seconds is not None), "collector_configuration_invalid")
    require(type(poll_seconds) in (int, float) and math.isfinite(poll_seconds)
            and 0.5 <= poll_seconds <= 60, "collector_configuration_invalid")


def safe_code(exc):
    code = getattr(exc, "code", None)
    return code if type(code) is str and code in _ERRORS else "collector_failed"


def base_report(status="starting"):
    return {"schema_version": 1, "provider": "wechat_postgres_collector",
            "status": status if status in _STATES else "collector_failed",
            "source_read_only": True, "receive_only": True, "messages_sent": 0,
            "model_calls": 0, "role_started": False, "plaintext_database_created": False,
            "keys_exported": False, "message_content_exported": False,
            "polls": 0, "events_committed": 0, "events_replayed": 0, "elapsed_seconds": 0.0,
            "source": source_report(None)}


def public_report(value):
    """Project only known fixed fields and bounded numeric counters."""
    result = base_report()
    if not isinstance(value, dict):
        result["status"] = "collector_failed"
        return result
    status = value.get("status")
    result["status"] = status if type(status) is str and status in _STATES else "collector_failed"
    for key in ("polls", "events_committed", "events_replayed"):
        number = value.get(key, 0)
        if type(number) is int and 0 <= number <= 2**63 - 1:
            result[key] = number
    elapsed = value.get("elapsed_seconds", 0.0)
    if type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= 2**31:
        result["elapsed_seconds"] = round(elapsed, 3)
    result["source"] = source_report(value.get("source"))
    return result


def collector_account(installation_id):
    # Separate the receive-only ledger from future role connector inboxes.
    require(identifier(installation_id), "collector_configuration_invalid")
    return stable_key("wechat-collector-v1", installation_id)


def verify_store(store):
    require(getattr(store, "backend", None) == "postgres" and store.db_path is None,
            "collector_store_required")
    with store.db() as db:
        for table in ("schema_migrations", "state", "core_events", "governance_policies", "actor_nodes"):
            require(db.execute("SELECT to_regclass(?)", (table,)).fetchone()[0] is not None,
                    "collector_schema_required")
        constraints = db.execute(
            "SELECT conname FROM pg_constraint WHERE convalidated AND contype='c' AND "
            "((conrelid=to_regclass('state') AND conname='state_encrypted') OR "
            "(conrelid=to_regclass('core_events') AND conname='core_events_encrypted'))"
        ).fetchall()
        require({row[0] for row in constraints} == {"state_encrypted", "core_events_encrypted"},
                "collector_schema_required")
        # This installation sentinel is encrypted but contains no personal record.
        epoch = store.state("governance-epoch", None, db)
        require(type(epoch) is int and epoch >= 0, "collector_key_invalid")


class Collector:
    """Exactly two bound conversations, one consumer, no model/runtime methods."""

    def __init__(self, store, gateway, account, *, clock=None, sleep=None):
        require(getattr(store, "backend", None) == "postgres" and store.db_path is None,
                "collector_store_required")
        require(identifier(account), "collector_configuration_invalid")
        self.store, self.gateway, self.account = store, gateway, account
        self.clock, self.sleep = clock or time.monotonic, sleep or asyncio.sleep
        self.cursor_key = stable_key("cursor", "wechat", account)
        self.report = base_report()
        self._lock = FileLock(str(store.runtime / (stable_key("wechat-collector", account) + ".lock")), timeout=0)

    def _role_quiet(self, db=None):
        for key in RUN_KEYS:
            run = self.store.state(key, {}, db)
            require(isinstance(run, dict), "collector_role_conflict")
            if run.get("status") != "active":
                continue
            accounts = run.get("accounts", [])
            require(isinstance(accounts, list), "collector_role_conflict")
            if key == "core-run:wechat" or any(
                    isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] == "wechat"
                    for pair in accounts):
                raise AssistantError("collector_role_conflict", "微信角色正在运行，采集器保持停止")

    def _scopes(self, db=None):
        connector = self.gateway.connector
        require(connector.platform == "wechat" and connector.account == self.account,
                "collector_scope_invalid")
        require(getattr(connector, "read_only_collection", False) is True
                and connector.capabilities() == [], "collector_source_writable")
        bindings = connector.bindings
        require(bindings.store is self.store and bindings.account == self.account, "collector_scope_invalid")
        state = self.store.state(bindings.key, None, db)
        require(isinstance(state, dict) and state.get("identity") == bindings.identity,
                "collector_scope_invalid")
        scopes = state.get("conversations")
        require(isinstance(scopes, dict) and len(scopes) == 2, "collector_scope_invalid")
        require(all(isinstance(value, dict) and value.get("enabled") is True for value in scopes.values()),
                "collector_scope_invalid")
        require({value.get("kind") for value in scopes.values()} == {"private", "group"}
                and len({value.get("target_ref") for value in scopes.values()}) == 2,
                "collector_scope_invalid")
        return scopes

    async def poll_once(self):
        self._role_quiet()
        self.report["source"] = source_report(await self.gateway.pump_once())
        self._scopes()
        cursor = self.store.state(self.cursor_key, {})
        incoming, next_cursor = await self.gateway.connector.receive(cursor)
        require(isinstance(incoming, (list, tuple)) and len(incoming) <= 100 and isinstance(next_cursor, dict),
                "collector_scope_invalid")
        inserted = replayed = 0
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._role_quiet(db)
            scopes = self._scopes(db)
            require(self.store.state(self.cursor_key, {}, db) == cursor, "collector_cursor_conflict")
            for event in incoming:
                require(isinstance(event, Event) and (event.platform, event.account) == ("wechat", self.account),
                        "collector_scope_invalid")
                binding = scopes.get(event.conversation)
                require(binding is not None and event.visibility == binding["kind"], "collector_scope_invalid")
                meta = event.metadata.get("wechat", {})
                require(meta.get("target_ref") == binding["target_ref"]
                        and meta.get("binding_revision") == binding["revision"], "collector_scope_invalid")
                receipt = events.ingest(self.store, event)
                require(not receipt.get("changed"), "collector_event_conflict")
                inserted += int(receipt["new"])
                replayed += int(not receipt["new"])
            self.store.set_state(self.cursor_key, next_cursor, db)
        self.report["polls"] += 1
        self.report["events_committed"] += inserted
        self.report["events_replayed"] += replayed
        if self.report["source"]["state"] == "blocked":
            code = self.report["source"]["code"] or "collector_failed"
            raise AssistantError(code, "只读来源需要人工检查，已提交收件的游标保持可恢复")
        return public_report(self.report)

    async def run(self, *, once=False, duration_seconds=None, poll_seconds=1.0, emit=None):
        validate_options(once, duration_seconds, poll_seconds)
        try:
            self._lock.acquire()
        except Timeout:
            raise AssistantError("collector_already_running", "已有独立微信采集器运行") from None
        started = self.clock()
        last_signature, last_emitted = None, started

        def emit_status(status):
            nonlocal last_signature, last_emitted
            self.report["status"] = status
            now = self.clock()
            self.report["elapsed_seconds"] = now - started
            report = public_report(self.report)
            # Poll counts, elapsed time, and a retry countdown alone do not flood
            # an idle user's terminal. Changed source state/counters are useful.
            signature = (status, report["events_committed"], report["events_replayed"],
                         tuple((key, value) for key, value in report["source"].items()
                               if key != "retry_in_seconds"))
            if emit and (signature != last_signature or now - last_emitted >= 60
                         or status in {"complete", "interrupted"} | _ERRORS):
                emit(report)
                last_signature, last_emitted = signature, now

        try:
            self._role_quiet()
            self.report["source"] = source_report(await self.gateway.start())
            self._scopes()
            emit_status("ready")
            while True:
                await self.poll_once()
                emit_status("collecting")
                remaining = None if duration_seconds is None else duration_seconds - (self.clock() - started)
                if once or remaining is not None and remaining <= 0:
                    break
                await self.sleep(poll_seconds if remaining is None else min(poll_seconds, remaining))
                if duration_seconds is not None and self.clock() - started >= duration_seconds:
                    break
        except asyncio.CancelledError:
            self.report["status"] = "interrupted"
            raise
        except Exception as exc:
            self.report["status"] = safe_code(exc)
            raise
        finally:
            try:
                await asyncio.wait_for(self.gateway.close(), timeout=10)
                if hasattr(self.gateway, "status"):
                    self.report["source"] = source_report(self.gateway.status())
                if self.report["status"] in {"interrupted"} | _ERRORS:
                    emit_status(self.report["status"])
            except Exception:
                emit_status("collector_cleanup_failed")
                raise AssistantError("collector_cleanup_failed", "采集器未能确认进程清理") from None
            finally:
                self._lock.release()
        emit_status("complete")
        return public_report(self.report)
