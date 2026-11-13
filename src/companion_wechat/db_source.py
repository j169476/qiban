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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .db_probe import make_sources, page_one, resolve_config_targets, safe_db_path, validate_config
from .db_text import short_text
from .db_triggers import message as triggered_message

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


def _contact_kind(own, username, kind, flags, deleted, verified, in_group):
    if not _username(username) or username == own:
        return None
    if deleted != 0 or verified != 0 or type(flags) is not int:
        return None
    if username.endswith("@chatroom"):
        return "group" if kind == 2 and in_group == 1 else None
    return "private" if kind == 1 and flags & 1 else None

def contact_page(connection, own, kind, after, limit):
    if (kind not in ("private", "group") or (after != "" and not _username(after))
            or type(limit) is not int or not 1 <= limit <= 100):
        raise SourceError("source_configuration_invalid")
    rows = connection.execute(
        "SELECT username, local_type, flag, delete_flag, verify_flag, is_in_chat_room, remark, nick_name "
        "FROM contact WHERE username COLLATE BINARY > ? AND length(username) <= 256 "
        "GROUP BY username COLLATE BINARY HAVING count(*) = 1 AND local_type = ? "
        "ORDER BY username COLLATE BINARY LIMIT ?",
        (after, 2 if kind == "group" else 1, limit + 1),
    ).fetchall()
    page = rows[:limit]
    items = []
    for username, *fields, remark, nickname in page:
        if _contact_kind(own, username, *fields) == kind:
            name = next((v[:200] for v in (remark, nickname) if isinstance(v, str) and v), "")
            items.append({"username": username, "name": name})
    return {"items": items, "next": page[-1][0] if len(rows) > limit else None}


def current_member_ids(connection, own, kind):
    """Read one consistent directory image without names or message payloads."""
    if kind not in ("private", "group"):
        raise SourceError("source_configuration_invalid")
    cursor = connection.execute(
        "SELECT username,local_type,flag,delete_flag,verify_flag,is_in_chat_room FROM contact "
        "WHERE length(username) BETWEEN 1 AND 256 GROUP BY username COLLATE BINARY "
        "HAVING count(*)=1 AND local_type=? ORDER BY username COLLATE BINARY",
        (1 if kind == "private" else 2,))
    try:
        return tuple(name for name, *fields in cursor if _contact_kind(own, name, *fields) == kind)
    finally:
        cursor.close()


def verify_contacts(connection, own, targets):
    """Verify only the selected IDs, against a single contact database snapshot."""
    if not isinstance(targets, list):
        raise SourceError("source_configuration_invalid")
    selected = {}
    for target in targets:
        if (not isinstance(target, dict) or set(target) != {"kind", "username"}
                or target["kind"] not in ("private", "group") or not _username(target["username"])
                or target["username"] in selected):
            raise SourceError("source_configuration_invalid")
        selected[target["username"]] = target["kind"]
    ids = list(selected)
    current = set()
    connection.execute("BEGIN")
    try:
        for offset in range(0, len(ids), 500):
            batch = ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                "SELECT username,local_type,flag,delete_flag,verify_flag,is_in_chat_room FROM contact "
                f"WHERE username COLLATE BINARY IN ({placeholders}) GROUP BY username COLLATE BINARY "
                "HAVING count(*) = 1", batch).fetchall()
            for username, *flags in rows:
                if _contact_kind(own, username, *flags) == selected[username]:
                    current.add(username)
    finally:
        connection.execute("ROLLBACK")
    return {"verified": len(current), "unavailable": [i for i in ids if i not in current]}


def _integer(value, low=0):
    return type(value) is int and low <= value <= 2**63 - 1


def validate_enrollment(value):
    fields = {"schema_version", "epoch", "account_id", "account_directory_id", "self_username", "targets", "since"}
    if (not isinstance(value, dict) or not fields <= set(value) or set(value) - fields - {"display_names","target_since"}
            or type(value["schema_version"]) is not int):
        raise SourceError("source_enrollment_invalid")
    if (value["schema_version"] not in (1, 2) or not isinstance(value["epoch"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", value["epoch"])
            or not _integer(value["since"], 1) or not _username(value["self_username"])
            or value["self_username"].endswith("@chatroom")
            or not isinstance(value["account_directory_id"], str) or not _HEX.fullmatch(value["account_directory_id"])
            or value["account_id"] != "wechat-db:" + hashlib.sha256(value["self_username"].encode()).hexdigest()):
        raise SourceError("source_enrollment_invalid")
    targets = value["targets"]
    if not isinstance(targets, dict) or (not targets and value["schema_version"] == 1):
        raise SourceError("source_enrollment_invalid")
    if value["schema_version"] == 2:
        since = value.get("target_since")
        if not isinstance(since, dict) or set(since) != set(targets) or not all(_integer(v, 1) for v in since.values()):
            raise SourceError("source_enrollment_invalid")
    elif "target_since" in value:
        raise SourceError("source_enrollment_invalid")
    identities = set()
    for alias, target in targets.items():
        if (not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", alias)
                or not isinstance(target, dict) or set(target) != {"kind", "username"}
                or target["kind"] not in ("private", "group")
                or alias in {"owner_main", "family"} and target["kind"] != {"owner_main": "private", "family": "group"}[alias]
                or not _username(target["username"]) or target["username"] == value["self_username"]
                or (target["kind"] == "group") != target["username"].endswith("@chatroom")
                or target["username"] in identities):
            raise SourceError("source_enrollment_invalid")
        identities.add(target["username"])
    names = value.get("display_names", {})
    if (not isinstance(names, dict) or set(names) - identities - {value["self_username"]}
            or any(not isinstance(name, str) or len(name) > 200 for name in names.values())):
        raise SourceError("source_enrollment_invalid")
    return _copy(value)


def validate_checkpoint(value, enrollment):
    if (not isinstance(value, dict) or set(value) - {"schema_version", "epoch", "sources", "next_source"}
            or not {"schema_version", "epoch", "sources"} <= set(value)
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["epoch"] != enrollment["epoch"] or not isinstance(value["sources"], dict)
            or not value["sources"] and enrollment["schema_version"] == 1
            or value.get("next_source") is not None and value["next_source"] not in value["sources"]):
        raise SourceError("source_checkpoint_invalid")
    for key, item in value["sources"].items():
        if (not isinstance(item, dict) or set(item) != {"shard", "target_ref", "salt", "anchor"}
                or not isinstance(item["shard"], str) or not _SHARD.fullmatch(item["shard"])
                or not isinstance(item["target_ref"], str) or item["target_ref"] not in enrollment["targets"]
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
    display_names: dict = field(default_factory=dict)
    open_contacts: object = None
    schema_versions: tuple = ()
    shards: dict = field(default_factory=dict)


@contextmanager
def _native_prepare(config, enrollment):
    from .db_crypto import verify_key
    from .db_probe import Target
    from .db_sqlite import open_readonly
    from .db_text import require_decoder
    from .db_windows import NativeProcess, discover_wechat, extract_keys

    paths = validate_config(config, allow_empty=bool(enrollment and enrollment.get("schema_version")==2))
    require_decoder()
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
        contact_keys = keys

        def open_contacts():
            return open_readonly(contact, contact_keys.key(selected))

        with open_contacts() as connection:
            own = self_username(connection, account.name)
            if enrollment is not None and own != enrollment["self_username"]:
                # Refuse before opening message shards or acquiring their keys.
                raise SourceError("source_identity_changed")
            if enrollment is None:
                targets = resolve_config_targets(connection, config)
            elif enrollment["schema_version"] == 2:
                targets = [Target(alias, t["username"]) for alias, t in enrollment["targets"].items()]
            else:
                targets = []
                for alias in enrollment["targets"]:
                    username = enrollment["targets"][alias]["username"]
                    rows = connection.execute(
                        "SELECT DISTINCT username COLLATE BINARY FROM contact "
                        "WHERE username=? COLLATE BINARY LIMIT 2", (username,),
                    ).fetchall()
                    if len(rows) != 1 or rows[0][0] != username:
                        raise SourceError("source_identity_changed")
                    targets.append(Target(alias, username))
            names = {}
            columns = {r[1] for r in connection.execute("PRAGMA table_info(contact)")}
            if {"remark", "nick_name"} <= columns:
                identifiers = [own, *(target.username for target in targets)]
                for offset in range(0, len(identifiers), 500):
                    batch = identifiers[offset:offset + 500]
                    rows = connection.execute("SELECT username,remark,nick_name FROM contact WHERE username COLLATE BINARY IN ("
                                              + ",".join("?" for _ in batch) + ")", batch).fetchall()
                    for username, remark, nickname in rows:
                        name = remark or nickname or ""
                        if isinstance(name, str):
                            names[username] = name[:200]
        files = _list_shards(account)
        process = stack.enter_context(NativeProcess(client.pid))
        pages = {f"shard_{i}": page_one(path) for i, path in enumerate(files)}
        keys = stack.enter_context(extract_keys(process, pages, verify_key))
        sources, versions, shards = [], [], {}
        for i, path in enumerate(files):
            connection = stack.enter_context(open_readonly(path, keys.key(f"shard_{i}")))
            shards[path] = connection
            if enrollment and enrollment["schema_version"] == 2:
                versions.append((connection, connection.execute("PRAGMA schema_version").fetchone()[0]))
            sources.extend(make_sources(connection, path, targets))
        if not versions and any(not any(s.target.alias == target.alias for s in sources) for target in targets):
            raise SourceError("source_identity_changed")
        yield Prepared(client, account, own, targets, sources, tuple(path.name for path in files), names, open_contacts, tuple(versions), shards)


def _anchor(row):
    if row is None:
        return None
    if len(row) != 3 or any(not _integer(value, 1) for value in row):
        raise SourceError("source_pending_at_enrollment")
    return dict(zip(("local_id", "server_id", "create_time"), row))


def enrollment_identity(value):
    """Display metadata is never part of account, cursor or send identity."""
    return {key: item for key, item in validate_enrollment(value).items() if key != "display_names"}


class SourceSession:
    """Keys/connections live only inside this context in the private worker."""

    def __init__(self, config, enrollment=None, checkpoint=None, *, prepare_fn=None, clock=time.time,
                 state_fn=None, recover_missing_history=False, authorized_since=None):
        if type(recover_missing_history) is not bool:
            raise SourceError("source_configuration_invalid")
        if authorized_since is not None and (not _integer(authorized_since,1) or enrollment is not None):
            raise SourceError("source_configuration_invalid")
        self._authorized_since = authorized_since
        self._recover_missing_history = recover_missing_history
        self._missing_sources = set()
        if (enrollment is None) != (checkpoint is None):
            raise SourceError("source_configuration_invalid")
        self._config, self._prepare = config, prepare_fn or _native_prepare
        self._enrollment = validate_enrollment(enrollment) if enrollment is not None else None
        self._checkpoint = validate_checkpoint(checkpoint, self._enrollment) if checkpoint is not None else None
        self._clock, self._stack, self._prepared, self._sources = clock, None, None, {}
        self._new_sources = set()
        self._dormant = set()
        self._member_page = None
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
            targets = {t.alias: {"kind": "group" if t.username.endswith("@chatroom") else "private",
                                 "username": t.username} for t in prepared.targets}
            if self._enrollment is None:
                self._enrollment = validate_enrollment({
                    "schema_version": 1, "epoch": uuid.uuid4().hex, "account_id": account_id,
                    "account_directory_id": directory_id, "self_username": prepared.self_username,
                    "targets": targets, "since": self._authorized_since or int(self._clock()),
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
            if getattr(prepared, "display_names", None):
                self._enrollment["display_names"] = dict(prepared.display_names)
            else:
                self._enrollment.pop("display_names", None)
            for source in prepared.sources:
                relative = source.path.relative_to(prepared.account / "db_storage").as_posix()
                if not _SHARD.fullmatch(relative) or source.target.alias not in targets:
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
                    if self._enrollment["schema_version"] == 2:
                        anchor = self._before_since(source, self._enrollment["target_since"][source.target.alias])
                    elif initial and self._authorized_since is not None:
                        anchor = self._before_since(source,self._authorized_since)
                    else:
                        anchor = self._latest_anchor(source) if initial else None
                    if not initial:
                        self._new_sources.add(key)
                    self._checkpoint["sources"][key] = {
                        "shard": relative, "target_ref": source.target.alias, "salt": seal, "anchor": anchor,
                    }
                elif item["salt"] != seal:
                    raise SourceError("source_generation_changed")
                self._verify_read_anchor(key, source, self._checkpoint["sources"][key]["anchor"])
            self._dormant = set(self._checkpoint["sources"]) - set(self._sources)
            for key in self._dormant:
                item = self._checkpoint["sources"][key]
                if (self._enrollment["schema_version"] != 2 or not self._recover_missing_history
                        or Path(item["shard"]).name not in prepared.catalog):
                    raise SourceError("source_generation_changed")
                shard = safe_db_path(prepared.account, Path(item["shard"]))
                if hashlib.sha256(page_one(shard)[:16]).hexdigest() != item["salt"]:
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
    def _before_since(source, since):
        # Find the earliest eligible row, even if later inserts have older timestamps.
        # Inspect metadata only. An unfinished row must still be eligible for a later read.
        source.connection.execute("BEGIN")
        try:
            first = source.connection.execute(
                f'SELECT local_id FROM "{source.table}" WHERE create_time>=? OR create_time IS NULL OR create_time<=0 ORDER BY local_id LIMIT 1',
                (since,)).fetchone()
            if first is None:
                return SourceSession._latest_anchor(source)
            return _anchor(source.connection.execute(
                f'SELECT local_id,server_id,create_time FROM "{source.table}" WHERE local_id<? AND server_id>0 AND create_time>0 '
                'ORDER BY local_id DESC LIMIT 1', (first[0],)).fetchone())
        finally:
            source.connection.execute("ROLLBACK")

    @staticmethod
    def _latest_anchor(source):
        row = source.connection.execute(
            f'SELECT local_id,server_id,create_time FROM "{source.table}" ORDER BY local_id DESC LIMIT 1'
        ).fetchone()
        return _anchor(row)

    @staticmethod
    def _verify_anchor(source, anchor, *, allow_missing=False):
        if anchor is None:
            return True
        row = source.connection.execute(
            f'SELECT local_id,server_id,create_time FROM "{source.table}" WHERE local_id=?',
            (anchor["local_id"],),
        ).fetchone()
        if row is None and allow_missing:
            return False
        if row is None or tuple(row) != tuple(anchor[k] for k in ("local_id", "server_id", "create_time")):
            raise SourceError("source_anchor_changed")
        return True

    def _verify_read_anchor(self, key, source, anchor):
        present = self._verify_anchor(source, anchor, allow_missing=self._recover_missing_history)
        if not present:
            self._missing_sources.add(key)
        return present

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

    def freeze_window(self, checkpoint):
        """Freeze per-source upper anchors without reading any message body.

        The caller retains this window until all its pages are durably stored.
        New records above these anchors belong to a later window. This proves
        only the range visible in the client's database, not remote completeness.
        """
        self._require_open()
        checkpoint = validate_checkpoint(checkpoint, self._enrollment)
        if set(checkpoint["sources"]) != self._sources.keys() | self._dormant:
            raise SourceError("source_checkpoint_invalid")
        sources = {}
        self._missing_sources.clear()
        for key, item in checkpoint["sources"].items():
            if key in self._dormant:
                sources[key] = {**_copy(item), "upper": item["anchor"]}
                continue
            source = self._sources[key]
            self._check_generation(source, item)
            if any(item[k] != self._checkpoint["sources"][key][k] for k in ("shard", "target_ref", "salt")):
                raise SourceError("source_generation_changed")
            source.connection.execute("BEGIN")
            try:
                self._verify_read_anchor(key, source, item["anchor"])
                upper = self._latest_anchor(source)
                if item["anchor"] and (upper is None or upper["local_id"] < item["anchor"]["local_id"]):
                    raise SourceError("source_anchor_changed")
                sources[key] = {**_copy(item), "upper": upper}
            finally:
                source.connection.execute("ROLLBACK")
        return {"epoch": self._enrollment["epoch"], "sources": sources}

    def _window_bounds(self, window, checkpoint):
        if (not isinstance(window, dict) or set(window) != {"epoch", "sources"}
                or window["epoch"] != self._enrollment["epoch"] or not isinstance(window["sources"], dict)
                or set(window["sources"]) != self._sources.keys() | self._dormant):
            raise SourceError("source_checkpoint_invalid")
        lower_sources, upper_sources = {}, {}
        for key, item in window["sources"].items():
            if not isinstance(item, dict) or set(item) != {"shard", "target_ref", "salt", "anchor", "upper"}:
                raise SourceError("source_checkpoint_invalid")
            lower_sources[key] = {k: v for k, v in item.items() if k != "upper"}
            upper_sources[key] = {**lower_sources[key], "anchor": item["upper"]}
        for sources in (lower_sources, upper_sources):
            validate_checkpoint({"schema_version": 1, "epoch": window["epoch"], "sources": sources}, self._enrollment)
        bounds = {}
        for key, item in window["sources"].items():
            current = checkpoint["sources"].get(key)
            if current is None or any(current[k] != item[k] for k in ("shard", "target_ref", "salt")):
                raise SourceError("source_generation_changed")
            low = item["anchor"]["local_id"] if item["anchor"] else 0
            high = item["upper"]["local_id"] if item["upper"] else 0
            position = current["anchor"]["local_id"] if current["anchor"] else 0
            if not low <= position <= high:
                raise SourceError("source_checkpoint_invalid")
            for endpoint in (item["anchor"], item["upper"]):
                if endpoint and position == endpoint["local_id"] and current["anchor"] != endpoint:
                    raise SourceError("source_anchor_changed")
            bounds[key] = high
        return bounds

    def _require_online(self):
        self._require_open()
        if not self._state(self._prepared.client.pid).get("window_count"):
            raise SourceError("client_not_running")

    def enrolled_members(self):
        self._require_online()
        if any(db.execute("PRAGMA schema_version").fetchone()[0] != v for db, v in self._prepared.schema_versions):
            raise SourceError("source_catalog_changed")
        with self._prepared.open_contacts() as connection:
            result = verify_contacts(connection, self._enrollment["self_username"],
                                     list(self._enrollment["targets"].values()))
        self._require_online()
        return result

    def member_page(self, kind, cursor, *, find_chats=False):
        self._require_online()
        if cursor is None:
            self._member_page = None
            with self._prepared.open_contacts() as db:
                ids = current_member_ids(db, self._enrollment["self_username"], kind)
            tables = None
            if find_chats:
                tables = {name for db in self._prepared.shards.values() for name, in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")}
            offset = 0
        else:
            saved = self._member_page
            self._member_page = None
            if not saved or (kind, cursor, find_chats) != saved[:3]:
                raise SourceError("source_configuration_invalid")
            _, _, _, ids, offset, tables = saved
        page = ids[offset:offset + 256]
        end = offset + len(page)
        next_cursor = uuid.uuid4().hex if end < len(ids) else None
        self._require_online()
        if next_cursor:
            self._member_page = (kind, next_cursor, find_chats, ids, end, tables)
        result = {"ids": list(page), "next": next_cursor}
        if find_chats:
            from .db_probe import message_table
            result["chats"] = [name for name in page if message_table(name) in tables]
        return result

    def member_kind(self, username):
        """Return current private/group membership for one ID, without opening a message table."""
        self._require_online()
        if not _username(username):
            raise SourceError("source_configuration_invalid")
        with self._prepared.open_contacts() as connection:
            rows = connection.execute(
                "SELECT local_type,flag,delete_flag,verify_flag,is_in_chat_room FROM contact "
                "WHERE username=? COLLATE BINARY LIMIT 2",(username,)).fetchall()
        return _contact_kind(self._enrollment["self_username"], username, *rows[0]) if len(rows) == 1 else None

    def directory_page(self, kind, after, limit):
        self._require_online()
        with self._prepared.open_contacts() as connection:
            result = contact_page(connection, self._enrollment["self_username"], kind, after, limit)
        self._require_online()
        return result

    def member_source(self, username, kind, since, checkpoint=None):
        """Borrow this authenticated account's open shards for one conversation."""
        from .db_probe import Target
        self._require_online()
        if kind not in ("private","group") or not _username(username) or not _integer(since, 1):
            raise SourceError("source_configuration_invalid")
        alias = "target_" + hashlib.sha256(username.encode()).hexdigest()[:32]
        enrollment = {**self._enrollment, "schema_version":2, "targets":{alias:{"username":username,"kind":kind}},
                      "target_since":{alias:since}, "since":since}
        enrollment.pop("display_names", None)
        if checkpoint is None:
            checkpoint = {"schema_version":1,"epoch":enrollment["epoch"],"sources":{}}
        parent = self._prepared
        @contextmanager
        def prepare(_config, _saved):
            self._require_open()
            targets = [Target(alias,username)]
            sources = [s for path,db in parent.shards.items() for s in make_sources(db,path,targets)]
            yield Prepared(parent.client,parent.account,parent.self_username,targets,sources,parent.catalog,
                           open_contacts=parent.open_contacts,shards=parent.shards)
        return SourceSession({},enrollment,checkpoint,prepare_fn=prepare,state_fn=self._state,recover_missing_history=True)

    def member_window(self, username, kind, since, checkpoint, window, limit):
        if self.member_kind(username) != kind:
            return {"present":False}
        with self.member_source(username,kind,since,checkpoint) as child:
            start = child.snapshot()["checkpoint"]
            if window is None:
                window = child.freeze_window(start)
            else:
                sources = dict(window["sources"])
                for key,item in start["sources"].items():
                    sources.setdefault(key,{**item,"upper":item["anchor"]})
                window = {**window,"sources":sources}
            data = child.read_window(start,window,limit=limit)
        if self.member_kind(username) != kind:
            return {"present":False}
        return {"present":True, "window":window, **data}

    def resolve_alias(self, hint):
        self._require_open()
        if not _username(hint):
            raise SourceError("source_configuration_invalid")
        if not self._state(self._prepared.client.pid).get("window_count"):
            raise SourceError("client_not_running")
        with self._prepared.open_contacts() as connection:
            rows = connection.execute(
                "SELECT DISTINCT username COLLATE BINARY FROM contact "
                "WHERE alias=? COLLATE BINARY OR username=? COLLATE BINARY LIMIT 2", (hint, hint)).fetchall()
        bound = {t["username"] for t in self._enrollment["targets"].values() if t["kind"] == "private"}
        username = rows[0][0] if len(rows) == 1 and rows[0][0] in bound else None
        return {"username": username}

    def read(self, checkpoint, *, limit=100):
        return self._read(checkpoint, limit=limit)

    def read_window(self, checkpoint, window, *, limit=100):
        return self._read(checkpoint, limit=limit, window=window)

    def _read(self, checkpoint, *, limit, window=None):
        self._require_open()
        # Rotate source pages so large directories cannot starve later chats.
        if type(limit) is not int or not 1 <= limit <= 100:
            raise SourceError("source_configuration_invalid")
        if self._state(self._prepared.client.pid).get("window_count", 0) == 0:
            raise SourceError("client_not_running")
        checkpoint = validate_checkpoint(checkpoint, self._enrollment)
        bounds = self._window_bounds(window, checkpoint) if window is not None else None
        if any(key not in self._sources and key not in self._dormant for key in checkpoint["sources"]):
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
        if bounds is not None:
            for key in self._dormant:
                checkpoint["sources"][key]["anchor"] = window["sources"][key]["upper"]
        rows, blocked, skipped = [], [], 0
        keys = sorted(self._sources)
        start = keys.index(checkpoint["next_source"]) if checkpoint.get("next_source") in keys else 0
        selected = (keys[start:] + keys[:start])[:limit]
        if len(keys) > limit:
            checkpoint["next_source"] = keys[(start + len(selected)) % len(keys)]
        row_quota = max(1, limit // max(1, len(selected)))
        scan_quota = min(100, max(1, 512 // max(1, len(selected))))
        for key in selected:
            source = self._sources[key]
            since = self._enrollment.get("target_since", {}).get(source.target.alias, self._enrollment["since"])
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
                self._verify_read_anchor(key, source, item["anchor"])
                if window is not None:
                    upper_exists = self._verify_read_anchor(key, source, window["sources"][key]["upper"])
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
                    'WHERE m.local_id>? AND m.local_id<=? ORDER BY m.local_id LIMIT ?',
                    (since, after, bounds[key] if bounds is not None else 2**63 - 1, scan_quota),
                ).fetchall()
                if window is not None and not upper_exists and not batch:
                    # The bound was previously authenticated and frozen. Advance
                    # only the cursor over its now-absent tail, never invent a row.
                    item["anchor"] = _copy(window["sources"][key]["upper"])
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
                    history = created < since
                    decoded = short_text(content) if not history else None
                    alias = source.target.alias
                    target_kind = self._enrollment["targets"][alias]["kind"]
                    if target_kind == "group" and decoded is not None and decoded.startswith(sender + ":\n"):
                        decoded = decoded[len(sender) + 2:]

                    def own_reference(identifier):
                        # Verify the quoted server ID against the same chat and
                        # the Name2Id sender; XML's claimed author alone is not evidence.
                        found = connection.execute(f'SELECT n.user_name FROM "{source.table}" m '
                            'LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id WHERE m.server_id=? LIMIT 2',
                            (identifier,)).fetchall()
                        return len(found) == 1 and found[0][0] == self._enrollment["self_username"]

                    text, trigger = triggered_message(kind, decoded, msgsource, self._enrollment["self_username"],
                                                      own_reference=own_reference, conversation=source.target.username)
                    if (kind & 0xFFFFFFFF) == 1 and not history and text is None:
                        blocked.append({"source": key, "reason": "text_unavailable"})
                        break
                    anchor = {"local_id": local_id, "server_id": server_id, "create_time": created}
                    if text is None or history:
                        skipped += 1
                    elif (target_kind == "private" and sender not in
                          {self._enrollment["self_username"], source.target.username}):
                        blocked.append({"source": key, "reason": "private_sender_mismatch"})
                        break
                    else:
                        if target_kind == "group" and text.startswith(sender + ":\n"):
                            text = text[len(sender) + 2:]
                        try:
                            occurred = datetime.fromtimestamp(created, timezone.utc).isoformat()
                        except (ValueError, OverflowError, OSError):
                            raise SourceError("source_schema_unsupported") from None
                        rows.append({"target_ref": source.target.username, "kind": target_kind,
                                     "server_id": str(server_id),
                                     "sender_ref": sender, "text": text, "occurred_at": occurred,
                                     "direction": "self" if sender == self._enrollment["self_username"] else "incoming",
                                     "trigger": "direct" if target_kind == "private" else trigger, "time_basis": "server"})
                        source_rows += 1
                    item["anchor"] = anchor
                    after = local_id
            finally:
                connection.execute("ROLLBACK")
        result = {"rows": rows, "next_checkpoint": checkpoint, "blocked": blocked, "skipped": skipped}
        if bounds is not None:
            result["window_complete"] = all(
                (checkpoint["sources"][key]["anchor"]["local_id"] if checkpoint["sources"][key]["anchor"] else 0) == upper
                for key, upper in bounds.items())
            if self._recover_missing_history:
                missing = {self._sources[key].target.alias for key in self._missing_sources}
                missing.update(checkpoint["sources"][key]["target_ref"] for key in self._dormant)
                result["history_status"] = {alias: {
                    "status": "incomplete" if alias in missing else "complete",
                    "reasons": ["checkpoint_unavailable"] if alias in missing else [],
                } for alias in self._enrollment["targets"]}
        return result

    def close(self):
        self._member_page = None
        stack, self._stack = self._stack, None
        try:
            if stack is not None:
                stack.close()
        finally:
            self._sources.clear()
            self._prepared = None

    def __exit__(self, *_):
        self.close()
