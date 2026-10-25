"""Platform-independent backups of committed data, including while roles are paused."""
import secrets
import threading
import time
from pathlib import Path

from .errors import AssistantError


class BackupMaintenance:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        self._lock = threading.Lock()
        self.busy = False
        self._last = {}

    def status(self):
        state = self.store.state("service-backup", {})
        return {key: state.get(key) for key in ("status", "last_saved_at", "next_at", "failures", "error",
                "bytes", "raw_bytes", "capacity_warning", "filename")} | {
                    "recovery_key_exported": bool(self.store.state("recovery-key-exported"))}

    def step(self):
        if getattr(self.store, "backend", None) != "postgres" or not self._lock.acquire(blocking=False):
            return None
        self.busy = True
        try:
            return self._step()
        finally:
            self.busy = False
            self._lock.release()

    def _step(self):
        now = self.clock()
        previous = self._last
        if previous.get("status") == "failed" and now < previous.get("next_at", 0):
            return None
        try:
            policy = self.store.read("policy")["data"]
            if not policy.get("auto_backup", True):
                return None
            previous = self.store.state("service-backup", {})
            if now < previous.get("next_at", 0):
                return None
            with self.store.lock:
                with self.store.db() as db:
                    revision = db.execute("SELECT revision FROM maintenance_revision WHERE singleton=TRUE").fetchone()[0]
                if previous.get("revision") == revision and previous.get("status") == "saved":
                    state = {**previous, "next_at": now + policy.get("backup_interval_minutes", 60) * 60}
                    self.store.set_state("service-backup", state)
                    self._last = state
                    return state
                saved = self.store.backup(force=True)
                state = {"status": "saved", "revision": revision, "last_saved_at": now,
                    "next_at": now + policy.get("backup_interval_minutes", 60) * 60,
                    "failures": 0, "error": None, "filename": Path(saved["path"]).name,
                    "bytes": Path(saved["path"]).stat().st_size, "raw_bytes": saved.get("raw_bytes"),
                    "capacity_warning": saved.get("raw_bytes", 0) >= 384 * 1024 * 1024}
                self.store.set_state("service-backup", state)
                self._last = state
            return state
        except Exception as exc:
            failures = previous.get("failures", 0) + 1
            # Stable codes only; never copy a filesystem path, database DSN or content.
            code = exc.code if isinstance(exc, AssistantError) and exc.code in {
                "backup_limit", "backup_invalid", "restore_pending", "asset_missing"} else "backup_failed"
            incident = previous.get("incident") if previous.get("status") == "failed" else None
            state = {**previous, "status": "failed", "failures": failures, "error": code,
                     "incident": incident or secrets.token_hex(16),
                     "next_at": now + min(3600, 60 * 2 ** min(failures - 1, 6))}
            self._last = state
            try:
                self.store.set_state("service-backup", state)
            except Exception:
                pass  # Database outage: retain the retry state in service memory until it returns.
            return state
