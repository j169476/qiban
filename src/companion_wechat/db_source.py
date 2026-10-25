"""Private, read-only source batches with caller-owned durable checkpoints.

No source method advances an acknowledged cursor. A caller must durably store
the returned batch and checkpoint together before using that checkpoint again.
This module belongs in the dedicated database worker, never the core process.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db_probe import make_sources, page_one, resolve_targets, safe_db_path, validate_config
from .db_text import short_text
from .db_triggers import message as triggered_message

_ALIASES = {"owner_main": "private", "family": "group"}
_HEX = re.compile(r"[0-9a-f]{64}")
_SHARD = re.compile(r"message/message_[0-9]+\.db")
_CODES = {
    "source_configuration_invalid", "source_closed", "source_catalog_changed", "source_generation_changed",
    "source_identity_changed", "self_identity_unverified", "source_schema_unsupported",
    "source_checkpoint_invalid", "source_anchor_changed", "source_pending_at_enrollment",
    "source_read_failed", "source_enrollment_invalid",
    "client_not_running", "key_not_found",
}


class SourceError(Exception):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in _CODES else "source_read_failed"
        super().__init__(self.code)


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _username(value):
    return (isinstance(value, str) and 0 < len(value) <= 256
            and not any(ord(c) < 32 for c in value))


def _integer(value, low=0):
    return type(value) is int and low <= value <= 2**63 - 1


def validate_enrollment(value):
    fields = {"schema_version", "epoch", "account_id", "account_directory_id", "self_username", "targets", "since"}
    if not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int:
        raise SourceError("source_enrollment_invalid")
    if (value["schema_version"] != 1 or not isinstance(value["epoch"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", value["epoch"])
            or not _integer(value["since"], 1) or not _username(value["self_username"])
            or value["self_username"].endswith("@chatroom")
            or not isinstance(value["account_directory_id"], str) or not _HEX.fullmatch(value["account_directory_id"])
            or value["account_id"] != "wechat-db:" + hashlib.sha256(value["self_username"].encode()).hexdigest()):
        raise SourceError("source_enrollment_invalid")
    targets = value["targets"]
    if not isinstance(targets, dict) or set(targets) != set(_ALIASES):
        raise SourceError("source_enrollment_invalid")
    for alias, kind in _ALIASES.items():
        target = targets[alias]
        if (not isinstance(target, dict) or set(target) != {"kind", "username"} or target["kind"] != kind
                or not _username(target["username"]) or target["username"] == value["self_username"]
                or (kind == "group") != target["username"].endswith("@chatroom")):
            raise SourceError("source_enrollment_invalid")
    return _copy(value)


def validate_checkpoint(value, enrollment):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "epoch", "sources"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["epoch"] != enrollment["epoch"] or not isinstance(value["sources"], dict)
            or not 1 <= len(value["sources"]) <= 32):
        raise SourceError("source_checkpoint_invalid")
    for key, item in value["sources"].items():
        if (not isinstance(item, dict) or set(item) != {"shard", "target_ref", "salt", "anchor"}
                or not isinstance(item["shard"], str) or not _SHARD.fullmatch(item["shard"])
                or not isinstance(item["target_ref"], str) or item["target_ref"] not in _ALIASES
                or key != item["shard"] + "|" + item["target_ref"]
                or not isinstance(item["salt"], str) or not _HEX.fullmatch(item["salt"])):
            raise SourceError("source_checkpoint_invalid")
        anchor = item["anchor"]
        if (anchor is not None and (not isinstance(anchor, dict)
                or set(anchor) != {"local_id", "server_id", "create_time"}
                or any(not _integer(anchor[k], 1) for k in anchor))):
            raise SourceError("source_checkpoint_invalid")
    return _copy(value)


def self_username(connection, account_name):
    """Directory suffix is only a candidate; contact identity must confirm it.

    The directory convention is documented by wechatauto-replica's wxid
    property. Unlike its fallback, absence/ambiguity here never proves self.
    """
    match = re.fullmatch(r"([A-Za-z0-9_-]{1,128})_[A-Za-z0-9]{4}", account_name)
    if not match:
        raise SourceError("self_identity_unverified")
    candidate = match[1]
    columns = {r[1] for r in connection.execute("PRAGMA table_info(contact)")}
    if not {"username", "alias"} <= columns:
        raise SourceError("self_identity_unverified")
    rows = connection.execute(
        "SELECT DISTINCT username COLLATE BINARY FROM contact "
        "WHERE username=? COLLATE BINARY OR alias=? COLLATE BINARY LIMIT 3", (candidate, candidate),
    ).fetchall()
    names = {row[0] for row in rows if _username(row[0]) and not row[0].endswith("@chatroom")}
    if len(rows) != 1 or len(names) != 1:
        raise SourceError("self_identity_unverified")
    return names.pop()


def _list_shards(account):
    directory = (account / "db_storage/message").resolve(strict=True)
    if not directory.is_relative_to(account):
        raise SourceError("source_identity_changed")
    files = sorted(p for p in directory.iterdir() if re.fullmatch(r"message_[0-9]+\.db", p.name))
    if not 1 <= len(files) <= 16 or any(p.resolve() != p or not p.is_file() for p in files):
        raise SourceError("source_catalog_changed")
    return files


@dataclass(repr=False)
class Prepared:
    client: object
    account: Path
    self_username: str
    targets: list
    sources: list
    catalog: tuple


@contextmanager
def _native_prepare(config, enrollment):
    from .db_crypto import verify_key
    from .db_probe import Target
    from .db_sqlite import open_readonly
    from .db_windows import NativeProcess, discover_wechat, extract_keys

    paths = validate_config(config)
    if enrollment is not None:
        enrollment = validate_enrollment(enrollment)
        # A process pin must never expand an enrolled reader into another
        # logged-in account. Narrow page authentication before any memory scan.
        paths = [path for path in paths
                 if hashlib.sha256(path.name.encode()).hexdigest() == enrollment["account_directory_id"]]
        if len(paths) != 1:
            raise SourceError("source_identity_changed")
    client = discover_wechat(pid=config.get("client_pid"))
    with ExitStack() as stack:
        process = stack.enter_context(NativeProcess(client.pid))
        contacts = {f"candidate_{i}": (path, safe_db_path(path, Path("contact/contact.db")))
                    for i, path in enumerate(paths)}
        pages = {alias: page_one(value[1]) for alias, value in contacts.items()}
        keys = stack.enter_context(extract_keys(process, pages, verify_key, require_all=False))
        if not keys.aliases():
            raise SourceError("key_not_found")
        if len(keys.aliases()) != 1:
            raise SourceError("source_identity_changed")
        selected = keys.aliases()[0]
        account, contact = contacts[selected]
        with open_readonly(contact, keys.key(selected)) as connection:
            own = self_username(connection, account.name)
            if enrollment is not None and own != enrollment["self_username"]:
                # Refuse before opening message shards or acquiring their keys.
                raise SourceError("source_identity_changed")
            if enrollment is None:
                targets = resolve_targets(connection, config["private_hint"], config["group_hint"])
            else:
                targets = []
                for alias in _ALIASES:
                    username = enrollment["targets"][alias]["username"]
                    rows = connection.execute(
                        "SELECT DISTINCT username COLLATE BINARY FROM contact "
                        "WHERE username=? COLLATE BINARY LIMIT 2", (username,),
                    ).fetchall()
                    if len(rows) != 1 or rows[0][0] != username:
                        raise SourceError("source_identity_changed")
                    targets.append(Target(alias, username))
        files = _list_shards(account)
        process = stack.enter_context(NativeProcess(client.pid))
        pages = {f"shard_{i}": page_one(path) for i, path in enumerate(files)}
        keys = stack.enter_context(extract_keys(process, pages, verify_key))
        sources = []
        for i, path in enumerate(files):
            connection = stack.enter_context(open_readonly(path, keys.key(f"shard_{i}")))
            sources.extend(make_sources(connection, path, targets))
        if any(not any(s.target.alias == alias for s in sources) for alias in _ALIASES):
            raise SourceError("source_identity_changed")
        yield Prepared(client, account, own, targets, sources, tuple(path.name for path in files))


def _anchor(row):
    if row is None:
        return None
    if len(row) != 3 or any(not _integer(value, 1) for value in row):
        raise SourceError("source_pending_at_enrollment")
    return dict(zip(("local_id", "server_id", "create_time"), row))


class SourceSession:
    """Keys/connections live only inside this context in the private worker."""

    def __init__(self, config, enrollment=None, checkpoint=None, *, prepare_fn=None, clock=time.time,
                 state_fn=None):
        if (enrollment is None) != (checkpoint is None):
            raise SourceError("source_configuration_invalid")
        self._config, self._prepare = config, prepare_fn or _native_prepare
        self._enrollment = validate_enrollment(enrollment) if enrollment is not None else None
        self._checkpoint = validate_checkpoint(checkpoint, self._enrollment) if checkpoint is not None else None
        self._clock, self._stack, self._prepared, self._sources = clock, None, None, {}
        self._new_sources = set()
        if state_fn is None:
            from .db_windows import window_state

            state_fn = window_state
        self._state = state_fn

    def __repr__(self):
        return "<SourceSession private read-only>"

    def __enter__(self):
        if self._stack is not None:
            raise SourceError("source_configuration_invalid")
        stack = ExitStack()
        try:
            prepared = stack.enter_context(self._prepare(self._config, self._enrollment))
            self._prepared = prepared
            directory_id = hashlib.sha256(prepared.account.name.encode()).hexdigest()
            account_id = "wechat-db:" + hashlib.sha256(prepared.self_username.encode()).hexdigest()
            targets = {t.alias: {"kind": _ALIASES[t.alias], "username": t.username} for t in prepared.targets}
            if self._enrollment is None:
                self._enrollment = validate_enrollment({
                    "schema_version": 1, "epoch": uuid.uuid4().hex, "account_id": account_id,
                    "account_directory_id": directory_id, "self_username": prepared.self_username,
                    "targets": targets, "since": int(self._clock()),
                })
                initial = True
                self._checkpoint = {"schema_version": 1, "epoch": self._enrollment["epoch"], "sources": {}}
            else:
                initial = False
                if (self._enrollment["account_id"] != account_id
                        or self._enrollment["account_directory_id"] != directory_id
                        or self._enrollment["self_username"] != prepared.self_username
                        or self._enrollment["targets"] != targets):
                    raise SourceError("source_identity_changed")
            for source in prepared.sources:
                relative = source.path.relative_to(prepared.account / "db_storage").as_posix()
                if not _SHARD.fullmatch(relative) or source.target.alias not in _ALIASES:
                    raise SourceError("source_identity_changed")
                key = relative + "|" + source.target.alias
                if key in self._sources:
                    raise SourceError("source_identity_changed")
                columns = {r[1] for r in source.connection.execute(f'PRAGMA table_info("{source.table}")')}
                if "create_time" not in columns:
                    raise SourceError("source_schema_unsupported")
                self._sources[key] = source
                seal = hashlib.sha256(page_one(source.path)[:16]).hexdigest()
                item = self._checkpoint["sources"].get(key)
                if item is None:
                    anchor = self._latest_anchor(source) if initial else None
                    if not initial:
                        self._new_sources.add(key)
                    self._checkpoint["sources"][key] = {
                        "shard": relative, "target_ref": source.target.alias, "salt": seal, "anchor": anchor,
                    }
                elif item["salt"] != seal:
                    raise SourceError("source_generation_changed")
                self._verify_anchor(source, self._checkpoint["sources"][key]["anchor"])
            if set(self._checkpoint["sources"]) != set(self._sources):
                raise SourceError("source_generation_changed")
            self._checkpoint = validate_checkpoint(self._checkpoint, self._enrollment)
            self._stack = stack
            return self
        except BaseException:
            stack.close()
            self._sources.clear()
            self._prepared = None
            raise

    @staticmethod
    def _latest_anchor(source):
        row = source.connection.execute(
            f'SELECT local_id,server_id,create_time FROM "{source.table}" ORDER BY local_id DESC LIMIT 1'
        ).fetchone()
        return _anchor(row)

    @staticmethod
    def _verify_anchor(source, anchor):
        if anchor is None:
            return
        row = source.connection.execute(
            f'SELECT local_id,server_id,create_time FROM "{source.table}" WHERE local_id=?',
            (anchor["local_id"],),
        ).fetchone()
        if row is None or tuple(row) != tuple(anchor[k] for k in ("local_id", "server_id", "create_time")):
            raise SourceError("source_anchor_changed")

    def snapshot(self):
        self._require_open()
        return {"enrollment": _copy(self._enrollment), "checkpoint": _copy(self._checkpoint)}

    def _require_open(self):
        if self._stack is None:
            raise SourceError("source_closed")

    def _check_generation(self, source, item):
        if (source.path.stat().st_ino != source.inode
                or hashlib.sha256(page_one(source.path)[:16]).hexdigest() != item["salt"]):
            raise SourceError("source_generation_changed")

    def read(self, checkpoint, *, limit=100):
        self._require_open()
        # At least one slot per source guarantees a busy private chat cannot
        # indefinitely starve the family conversation (or another shard).
        if type(limit) is not int or not len(self._sources) <= limit <= 100:
            raise SourceError("source_configuration_invalid")
        if self._state(self._prepared.client.pid).get("window_count", 0) == 0:
            raise SourceError("client_not_running")
        checkpoint = validate_checkpoint(checkpoint, self._enrollment)
        if any(key not in self._sources for key in checkpoint["sources"]):
            raise SourceError("source_generation_changed")
        files = _list_shards(self._prepared.account)
        if tuple(path.name for path in files) != self._prepared.catalog:
            raise SourceError("source_catalog_changed")
        # A reconnect may discover new shards. Repeated reads of an old ack must
        # seed them at zero every time, never at the current MAX(local_id).
        for key, initial in self._checkpoint["sources"].items():
            if key not in checkpoint["sources"]:
                if key not in self._new_sources:
                    raise SourceError("source_checkpoint_invalid")
                checkpoint["sources"][key] = _copy(initial)
        rows, blocked, skipped = [], [], 0
        row_quota = limit // len(self._sources)
        scan_quota = min(100, 512 // len(self._sources))
        for key, source in self._sources.items():
            source_rows = 0
            item = checkpoint["sources"][key]
            if (item["shard"] != self._checkpoint["sources"][key]["shard"]
                    or item["target_ref"] != self._checkpoint["sources"][key]["target_ref"]
                    or item["salt"] != self._checkpoint["sources"][key]["salt"]):
                raise SourceError("source_generation_changed")
            self._check_generation(source, item)
            connection = source.connection
            connection.execute("BEGIN")
            try:
                self._verify_anchor(source, item["anchor"])
                after = item["anchor"]["local_id"] if item["anchor"] else 0
                columns = {r[1] for r in connection.execute(f'PRAGMA table_info("{source.table}")')}
                metadata = ('CASE WHEN length(CAST(m.source AS BLOB))<=4096 THEN m.source END'
                            if "source" in columns else 'NULL')
                batch = connection.execute(
                    f'SELECT m.local_id,m.server_id,m.create_time,m.local_type,n.user_name,'
                    'CASE WHEN (m.local_type & 4294967295) IN (1,49) AND m.create_time>=? '
                    'AND length(CAST(m.message_content AS BLOB))<=4096 '
                    f'THEN m.message_content ELSE NULL END,{metadata} '
                    f'FROM "{source.table}" m LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id '
                    'WHERE m.local_id>? ORDER BY m.local_id LIMIT ?',
                    (self._enrollment["since"], after, scan_quota),
                ).fetchall()
                for raw in batch:
                    if source_rows >= row_quota:
                        break
                    local_id, server_id, created, kind, sender, content, msgsource = raw
                    if not _integer(local_id, 1) or local_id <= after:
                        raise SourceError("source_schema_unsupported")
                    reason = None
                    if not _integer(server_id, 1):
                        reason = "pending_server_id"
                    elif not _integer(created, 1):
                        reason = "pending_create_time"
                    elif not _username(sender):
                        reason = "pending_sender"
                    elif not _integer(kind, 1):
                        reason = "pending_message_type"
                    if reason:
                        blocked.append({"source": key, "reason": reason})
                        break
                    history = created < self._enrollment["since"]
                    decoded = short_text(content) if not history else None
                    alias = source.target.alias
                    if alias == "family" and decoded is not None and decoded.startswith(sender + ":\n"):
                        decoded = decoded[len(sender) + 2:]

                    def own_reference(identifier):
                        # Verify the quoted server ID against the same chat and
                        # the Name2Id sender; XML's claimed author alone is not evidence.
                        found = connection.execute(f'SELECT n.user_name FROM "{source.table}" m '
                            'LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id WHERE m.server_id=? LIMIT 2',
                            (identifier,)).fetchall()
                        return len(found) == 1 and found[0][0] == self._enrollment["self_username"]

                    text, trigger = triggered_message(kind, decoded, msgsource, self._enrollment["self_username"],
                                                      own_reference=own_reference)
                    if (kind & 0xFFFFFFFF) == 1 and not history and text is None:
                        blocked.append({"source": key, "reason": "text_unavailable"})
                        break
                    anchor = {"local_id": local_id, "server_id": server_id, "create_time": created}
                    if text is None or history:
                        skipped += 1
                    elif (alias == "owner_main" and sender not in
                          {self._enrollment["self_username"], source.target.username}):
                        blocked.append({"source": key, "reason": "private_sender_mismatch"})
                        break
                    else:
                        if alias == "family" and text.startswith(sender + ":\n"):
                            text = text[len(sender) + 2:]
                        try:
                            occurred = datetime.fromtimestamp(created, timezone.utc).isoformat()
                        except (ValueError, OverflowError, OSError):
                            raise SourceError("source_schema_unsupported") from None
                        rows.append({"target_ref": source.target.username, "kind": _ALIASES[alias],
                                     "server_id": str(server_id),
                                     "sender_ref": sender, "text": text, "occurred_at": occurred,
                                     "direction": "self" if sender == self._enrollment["self_username"] else "incoming",
                                     "trigger": "direct" if alias == "owner_main" else trigger, "time_basis": "server"})
                        source_rows += 1
                    item["anchor"] = anchor
                    after = local_id
            finally:
                connection.execute("ROLLBACK")
        return {"rows": rows, "next_checkpoint": checkpoint, "blocked": blocked, "skipped": skipped}

    def close(self):
        stack, self._stack = self._stack, None
        try:
            if stack is not None:
                stack.close()
        finally:
            self._sources.clear()
            self._prepared = None

    def __exit__(self, *_):
        self.close()
