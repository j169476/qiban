"""Platform-neutral reply permission records and connector discovery contract.

This module knows no platform client, UI, role, model, or service implementation.
Only authenticated management code may call update. Discovery never grants access.
"""
from __future__ import annotations

import math
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Protocol

from .contracts import stable_key
from .errors import AssistantError

PREFIX = "reply-authorization-v1:"
AUDIT_PREFIX = "reply-authorization-audit-v1:"
PLATFORMS = frozenset({"wechat", "qq"})


def require(condition, code="reply_authorization_invalid"):
    if not condition:
        raise AssistantError(code, "会话回复授权无效或已改变，请重新读取授权列表")


@dataclass(frozen=True)
class ReplyScope:
    platform: str
    account_identity: str
    kind: str
    target_id: str

    def __post_init__(self):
        require(self.platform in PLATFORMS and self.kind in {"private", "group"})
        # These are opaque, case-sensitive connector-verified IDs, never labels.
        require(all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,256}", v)
                    for v in (self.account_identity, self.target_id)))

    @property
    def id(self):
        return stable_key("reply-scope-v1", self.platform, self.account_identity, self.kind, self.target_id)


@dataclass(frozen=True)
class ReplyTarget:
    scope: ReplyScope
    label: str
    account_label: str
    restriction: str = ""


class ReplyTargetProvider(Protocol):
    def reply_targets(self) -> tuple[ReplyTarget, ...]:
        """Return independently verified scopes, without connecting or granting."""
        ...


class ReplyAuthorizations:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock

    def record(self, scope, *, db=None):
        require(isinstance(scope, ReplyScope))
        value = self.store.state(PREFIX + scope.id, None, db)
        if value is None:
            return {"id": scope.id, "scope": asdict(scope), "enabled": False, "version": None}
        require(isinstance(value, dict) and value.get("id") == scope.id and value.get("scope") == asdict(scope)
                and type(value.get("enabled")) is bool and isinstance(value.get("version"), str),
                "reply_authorization_corrupt")
        return value

    def records(self):
        with self.store.db() as db:
            rows = db.execute("SELECT key FROM state WHERE key LIKE ? ORDER BY key", (PREFIX + "%",))
            result = []
            for row in rows:
                value = self.store.state(row["key"], None, db)
                require(isinstance(value, dict) and isinstance(value.get("scope"), dict), "reply_authorization_corrupt")
                scope = ReplyScope(**value["scope"])
                require(row["key"] == PREFIX + scope.id, "reply_authorization_corrupt")
                result.append(self.record(scope, db=db))
            return result

    def version(self, scope, *, db=None, message_time=None):
        record = self.record(scope, db=db)
        from .reply_audience import effective_version
        from .reply_exclusion import usable

        if message_time is not None and not usable(message_time):
            # A supplied message time that cannot be trusted never becomes a permit.
            return None
        manual = record["enabled"] and record.get("member_present", True)
        if message_time is not None and message_time < record.get("messages_since", 0):
            manual = False
        return effective_version(self.store, scope, record["version"] if manual else None, db, message_time)

    def observe_member(self, scope, present, expected_version, db):
        """Keep the choice, but invalidate work accepted under a previous relationship."""
        require(scope.platform == "wechat" and type(present) is bool)
        record = self.record(scope, db=db)
        if not record["enabled"] or record["version"] != expected_version or record.get("member_present", True) == present:
            return
        updated = {**record, "member_present": present, "version": secrets.token_hex(16)}
        updated["messages_since"] = max(record.get("messages_since", 0), math.ceil(self.clock()))
        self.store.set_state(PREFIX + scope.id, updated, db)

    def check(self, scope, version, *, db=None):
        require(isinstance(version, str) and self.version(scope, db=db) == version, "reply_not_authorized")

    def update(self, scope, *, enabled, expected_version, actor, db=None):
        """Trusted management-only CAS; transports must authenticate before calling."""
        require(type(enabled) is bool and (expected_version is None or isinstance(expected_version, str)))
        require(isinstance(actor, str) and 0 < len(actor) <= 256)
        require(self.store.ledger_encode("reply-authorization-preflight", {}).startswith("enc1:"),
                "reply_authorization_store_unencrypted")
        if db is None:
            with self.store.lock, self.store.db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.update(scope, enabled=enabled, expected_version=expected_version, actor=actor, db=connection)
        previous = self.record(scope, db=db)
        require(previous["version"] == expected_version, "edit_conflict")
        if previous["version"] is not None and previous["enabled"] == enabled:
            return previous
        now, version = self.clock(), secrets.token_hex(16)
        result = {"id": scope.id, "scope": asdict(scope), "enabled": enabled, "version": version,
                  "updated_at": now, "updated_by": actor}
        self.store.set_state(PREFIX + scope.id, result, db)
        self.store.set_state(AUDIT_PREFIX + version, {"scope_id": scope.id, "actor": actor, "at": now,
            "enabled": enabled, "before": previous["version"], "after": version}, db)
        return result

    @contextmanager
    def submission(self, scope, version):
        # Management mutations use this same store lock. Hold it only around the
        # final synchronous submission, never while awaiting model/receipt work.
        with self.store.lock:
            self.check(scope, version)
            yield

    def reset_after_restore(self):
        from .reply_audience import reset
        with self.store.lock, self.store.db() as db:
            reset(self.store, db)
            for record in self.records():
                if record["enabled"]:
                    self.update(ReplyScope(**record["scope"]), enabled=False,
                                expected_version=record["version"], actor="restore", db=db)
