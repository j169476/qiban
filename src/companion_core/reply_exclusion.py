"""Explicit single-conversation reply exclusions that never pause a whole account.

An exclusion is stored per :class:`ReplyScope` (platform, receiving account,
private/group, connector-verified target). It takes precedence over both the
manual per-conversation grant and the account-wide "allow all" category, while
every other conversation of the same account keeps whatever it already had.

Three rules keep this a block and not a switch:

* While excluded, the scope has no effective permission at all, so an admitted
  reply, a frozen batch or a pending submission loses its permit immediately.
* Lifting never restores a previous permit: every change writes a new revision
  that is mixed into the permission token, so a ticket issued before the
  exclusion can never become valid again.
* A lifted scope only accepts messages the platform timestamped at or after the
  lift, in whole platform seconds. An unusable timestamp is never treated as
  "now", because that would turn an old message into a new one.

This module knows no platform client, UI, role or model. Only authenticated
management code may call :meth:`ReplyExclusions.update`.
"""

import math
import secrets
import time
from dataclasses import asdict
from datetime import datetime

from .contracts import stable_key
from .errors import AssistantError

PREFIX = "reply-exclusion-v1:"
AUDIT_PREFIX = "reply-exclusion-audit-v1:"


def require(condition, code="reply_exclusion_invalid"):
    if not condition:
        raise AssistantError(code, "单会话排除记录无效或已改变，请重新读取")


def blank(scope):
    """No exclusion was ever saved, so every existing reply rule still applies."""
    return {"id": scope.id, "scope": asdict(scope), "excluded": False, "version": None,
            "since": None, "not_before": None}


def seconds(value):
    """A message time in Unix seconds, or None when it cannot be trusted.

    Platforms report whole positive seconds; ISO strings keep their offset. A
    missing, non-finite, non-positive, unparsable or offset-less value returns
    None and is never replaced by the current clock.
    """
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) and value > 0 else None
    if not isinstance(value, str) or not value:
        return None
    try:
        if value.isascii() and value.isdigit():
            parsed = float(int(value))
        else:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                return None
            parsed = stamp.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def usable(value):
    """True only for a finite positive Unix second."""
    return type(value) in (int, float) and math.isfinite(value) and value > 0


class ReplyExclusions:
    """One durable record per scope; management writes are the only mutation."""

    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock

    def record(self, scope, *, db=None):
        value = self.store.state(PREFIX + scope.id, None, db)
        if value is None:
            return blank(scope)
        return self._validate(scope, value)

    @staticmethod
    def _validate(scope, value):
        require(isinstance(value, dict) and value.get("id") == scope.id
                and value.get("scope") == asdict(scope) and type(value.get("excluded")) is bool,
                "reply_exclusion_corrupt")
        version, since, not_before = value.get("version"), value.get("since"), value.get("not_before")
        require(version is None or isinstance(version, str), "reply_exclusion_corrupt")
        require(since is None or type(since) is int and since > 0, "reply_exclusion_corrupt")
        require(not_before is None or type(not_before) is int and not_before > 0, "reply_exclusion_corrupt")
        if value["excluded"]:
            require(version is not None and since is not None, "reply_exclusion_corrupt")
        elif version is not None:
            # A lifted scope always carries the second its reply eligibility restarts at.
            require(not_before is not None, "reply_exclusion_corrupt")
        return value

    def records(self):
        from .reply_authorization import ReplyScope

        with self.store.db() as db:
            rows = db.execute("SELECT key FROM state WHERE key LIKE ? ORDER BY key", (PREFIX + "%",))
            result = []
            for row in rows:
                value = self.store.state(row["key"], None, db)
                require(isinstance(value, dict) and isinstance(value.get("scope"), dict),
                        "reply_exclusion_corrupt")
                scope = ReplyScope(**value["scope"])
                require(row["key"] == PREFIX + scope.id, "reply_exclusion_corrupt")
                result.append(self._validate(scope, value))
            return result

    def gate(self, scope, token, *, db=None, message_time=None):
        """The permission token after this exclusion, or None when it must not reply."""
        record = self.record(scope, db=db)
        if record["excluded"] or token is None:
            return None
        if record["version"] is None:
            return token  # Never excluded: the caller's existing token, unchanged.
        if message_time is not None and not (usable(message_time) and message_time >= record["not_before"]):
            # An unusable or too-old message time never becomes a new message.
            return None
        return stable_key("reply-exclusion", token, record["version"])

    def accepts(self, scope, message_time, *, db=None):
        """Whether a message with this platform time may be answered at all."""
        record = self.record(scope, db=db)
        if record["excluded"]:
            return False
        if record["version"] is None:
            return True
        return usable(message_time) and message_time >= record["not_before"]

    def update(self, scope, *, excluded, expected_version, actor, db=None):
        """Trusted management-only CAS; transports must authenticate before calling."""
        require(type(excluded) is bool and (expected_version is None or isinstance(expected_version, str)))
        require(isinstance(actor, str) and 0 < len(actor) <= 256)
        require(self.store.ledger_encode("reply-exclusion-preflight", {}).startswith("enc1:"),
                "reply_exclusion_store_unencrypted")
        if db is None:
            with self.store.lock, self.store.db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.update(scope, excluded=excluded, expected_version=expected_version,
                                   actor=actor, db=connection)
        previous = self.record(scope, db=db)
        require(previous["version"] == expected_version, "edit_conflict")
        if previous["version"] is None and not excluded:
            # Lifting a scope that was never excluded must not invent a cutoff.
            return previous
        if previous["version"] is not None and previous["excluded"] == excluded:
            return previous  # Same state: no new revision, nothing to invalidate.
        now = math.ceil(self.clock())
        since, not_before = previous["since"], previous["not_before"]
        if excluded:
            since = max(since or 0, now)
        else:
            # Both transports report whole Unix seconds; never accept a message
            # timestamped in a second that started before this lift. The start also
            # never moves earlier than the exclusion it lifts or an earlier lift.
            not_before = max(not_before or 0, now, since or 0)
        version = secrets.token_hex(16)
        result = {"id": scope.id, "scope": asdict(scope), "excluded": excluded, "version": version,
                  "since": since, "not_before": not_before, "updated_at": self.clock(),
                  "updated_by": actor}
        self.store.set_state(PREFIX + scope.id, result, db)
        self.store.set_state(AUDIT_PREFIX + version, {
            "scope_id": scope.id, "actor": actor, "at": now, "excluded": excluded,
            "before": previous["version"], "after": version, "since": since,
            "not_before": not_before}, db)
        return result


def gate(store, scope, token, *, db=None, message_time=None):
    return ReplyExclusions(store).gate(scope, token, db=db, message_time=message_time)


def accepts(store, scope, message_time, *, db=None):
    return ReplyExclusions(store).accepts(scope, message_time, db=db)


def revision(store, scope, base, *, db=None):
    """Token that changes with every exclusion change, so frozen batches expire."""
    record = ReplyExclusions(store).record(scope, db=db)
    return base if record["version"] is None else stable_key("reply-exclusion-revision", base, record["version"])
