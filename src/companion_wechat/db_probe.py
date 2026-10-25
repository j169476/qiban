"""Bounded, diagnostic-only database observations. No core events or sending.

Queries only the two explicit targets. Private rows/identifiers stay in the
worker; reports contain counters and numbered synthetic test-marker results.
"""
from __future__ import annotations

import hashlib
import re
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path


class ProbeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(repr=False)
class Target:
    alias: str
    username: str = field(repr=False)


@dataclass(repr=False)
class Source:
    connection: object = field(repr=False)
    path: Path = field(repr=False)
    target: Target = field(repr=False)
    table: str = field(repr=False)
    baseline: int = 0
    salt: bytes = field(default=b"", repr=False)
    inode: int = 0


def local_path(value):
    if not isinstance(value, str) or not value or value.startswith(("\\\\", "//")):
        raise ProbeError("configuration_invalid")
    p = Path(value)
    if not p.is_absolute() or ".." in p.parts:
        raise ProbeError("configuration_invalid")
    return p.resolve(strict=True)


def validate_config(config):
    fields = {"account_dirs", "private_hint", "group_hint"}
    if (not isinstance(config, dict) or not fields <= set(config)
            or set(config) - fields - {"client_pid"}):
        raise ProbeError("configuration_invalid")
    if "client_pid" in config and (type(config["client_pid"]) is not int or not 0 < config["client_pid"] < 2**32):
        raise ProbeError("configuration_invalid")
    dirs = config["account_dirs"]
    if not isinstance(dirs, list) or not 1 <= len(dirs) <= 8:
        raise ProbeError("configuration_invalid")
    paths = [local_path(p) for p in dirs]
    if len(set(paths)) != len(paths) or any(not p.is_dir() for p in paths):
        raise ProbeError("configuration_invalid")
    for key in ("private_hint", "group_hint"):
        value = config[key]
        if not isinstance(value, str) or not 1 <= len(value) <= 128 or "\x00" in value:
            raise ProbeError("configuration_invalid")
    return paths


def safe_db_path(account, relative):
    path = (account / "db_storage" / relative).resolve(strict=True)
    if not path.is_relative_to(account.resolve()) or not path.is_file():
        raise ProbeError("database_path_invalid")
    return path


def page_one(path):
    with path.open("rb") as f:
        page = f.read(4096)
    if len(page) != 4096 or page.startswith(b"SQLite format 3\x00"):
        raise ProbeError("database_format_unsupported")
    return page


def resolve_targets(conn, private_hint, group_hint):
    """Exact matches only; never choose the first of ambiguous display names."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(contact)")}
    if not {"username", "alias", "nick_name", "remark"} <= columns:
        raise ProbeError("contact_schema_unsupported")
    private = conn.execute(
        "SELECT DISTINCT username COLLATE BINARY FROM contact WHERE username COLLATE BINARY=? "
        "OR alias COLLATE BINARY=? LIMIT 3",
        (private_hint, private_hint),
    ).fetchall()
    group = conn.execute(
        "SELECT DISTINCT username COLLATE BINARY FROM contact "
        "WHERE (nick_name COLLATE BINARY=? OR remark COLLATE BINARY=?) "
        "AND username LIKE '%@chatroom' LIMIT 3", (group_hint, group_hint),
    ).fetchall()
    users = []
    for alias, rows in (("owner_main", private), ("family", group)):
        names = {r[0] for r in rows if isinstance(r[0], str) and 1 <= len(r[0]) <= 256}
        if len(names) != 1:
            raise ProbeError("target_missing_or_ambiguous")
        username = names.pop()
        if (alias == "family") != username.endswith("@chatroom"):
            raise ProbeError("target_kind_mismatch")
        users.append(Target(alias, username))
    return users


def message_table(username):
    # WeChat's table naming convention; this is not used as an event identity.
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def make_sources(conn, path, targets):
    found = []
    for target in targets:
        table = message_table(target.username)
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if not {"local_id", "server_id", "local_type", "real_sender_id", "message_content"} <= columns:
            raise ProbeError("message_schema_unsupported")
        names = {r[1] for r in conn.execute("PRAGMA table_info(Name2Id)")}
        if "user_name" not in names:
            raise ProbeError("sender_schema_unsupported")
        baseline = conn.execute(f'SELECT COALESCE(MAX(local_id),0) FROM "{table}"').fetchone()[0]
        if type(baseline) is not int or baseline < 0:
            raise ProbeError("message_schema_unsupported")
        st = path.stat()
        found.append(Source(conn, path, target, table, baseline, page_one(path)[:16], st.st_ino))
    return found


def read_since_baseline(source, *, limit=2000):
    """Revisit bounded post-baseline rows, including pending rows later filled in.

    A capability experiment is not a production cursor. A fresh experiment
    establishes a fresh baseline; no historical body is read during startup.
    """
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise ProbeError("configuration_invalid")
    if source.path.stat().st_ino != source.inode or page_one(source.path)[:16] != source.salt:
        raise ProbeError("database_generation_changed")
    conn = source.connection
    conn.execute("BEGIN")
    try:
        maximum = conn.execute(f'SELECT COALESCE(MAX(local_id),0) FROM "{source.table}"').fetchone()[0]
        if maximum < source.baseline:
            raise ProbeError("database_generation_changed")
        rows = conn.execute(
            f'SELECT m.local_id,m.server_id,m.local_type,n.user_name,'
            'CASE WHEN m.local_type=1 AND length(CAST(m.message_content AS BLOB))<=4096 '
            'THEN m.message_content ELSE NULL END '
            f'FROM "{source.table}" m LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id '
            'WHERE m.local_id>? ORDER BY m.local_id LIMIT ?',
            (source.baseline, limit + 1),
        ).fetchall()
        if len(rows) > limit:
            raise ProbeError("observation_limit_exceeded")
        return rows
    finally:
        conn.execute("ROLLBACK")


def marker_number(content, prefix, alias, sender):
    from .db_text import short_text

    content = short_text(content)
    if content is None:
        return None
    # Some group formats include a stable sender identifier before the text.
    if alias == "family" and isinstance(sender, str) and content.startswith(sender + ":\n"):
        content = content[len(sender) + 2:]
    letter = "P" if alias == "owner_main" else "G"
    match = re.fullmatch(re.escape(prefix) + "-" + letter + r"-0([123])", content.strip())
    return int(match[1]) if match else None


class MarkerTracker:
    """Retains only row identities and test-marker numbers, never message text."""
    def __init__(self, owner_username, prefix):
        if not re.fullmatch(r"XLD-[A-F0-9]{8}", prefix):
            raise ProbeError("configuration_invalid")
        self.owner_username = owner_username
        self.prefix = prefix
        self.rows = set()
        self.identities = {}
        self.local_markers = {}
        self.unverified_rows = set()
        self.unstable_rows = set()
        self.targets = {alias: {"new_rows": 0, "marker_counts": [0, 0, 0],
                                "minimized_marker_counts": [0, 0, 0],
                                "unverified_sender_markers": 0, "unstable_id_markers": 0}
                        for alias in ("owner_main", "family")}

    def observe(self, shard, alias, row, minimized):
        local_id, server_id, local_type, sender, content = row
        if type(local_id) is not int or local_id < 1:
            raise ProbeError("message_schema_unsupported")
        local = (shard, alias, local_id)
        target = self.targets[alias]
        if local not in self.rows:
            if len(self.rows) >= 10000:
                raise ProbeError("observation_limit_exceeded")
            self.rows.add(local)
            target["new_rows"] += 1
        number = marker_number(content, self.prefix, alias, sender) if local_type == 1 else None
        if local in self.local_markers:
            if self.local_markers[local] != (number, sender, server_id):
                raise ProbeError("message_identity_conflict")
            return
        if number is None:
            return
        if sender != self.owner_username:
            if local not in self.unverified_rows:
                self.unverified_rows.add(local)
                target["unverified_sender_markers"] += 1
            return
        if type(server_id) is not int or server_id <= 0:
            if local not in self.unstable_rows:
                self.unstable_rows.add(local)
                target["unstable_id_markers"] += 1
            return
        self.local_markers[local] = (number, sender, server_id)
        identity = (alias, server_id)
        if identity in self.identities:
            if self.identities[identity] != number:
                raise ProbeError("message_identity_conflict")
            return
        self.identities[identity] = number
        target["marker_counts"][number - 1] += 1
        if minimized:
            target["minimized_marker_counts"][number - 1] += 1


def base_report():
    return {"schema_version": 1, "provider": "sqlcipher_readonly", "status": "preparing",
            "read_only": True, "messages_sent": 0, "keys_exported": False,
            "message_content_exported": False, "plaintext_database_created": False,
            "core_events_created": 0, "ui_modified": False}


@contextmanager
def prepare(config):
    from .db_crypto import verify_key
    from .db_sqlite import open_readonly
    from .db_windows import NativeProcess, discover_wechat, extract_keys

    paths = validate_config(config)
    client = discover_wechat(pid=config.get("client_pid"))
    with ExitStack() as stack:
        process = stack.enter_context(NativeProcess(client.pid))
        pages, contacts = {}, {}
        for index, path in enumerate(paths):
            contact = safe_db_path(path, Path("contact/contact.db"))
            name = f"candidate_{index}"
            contacts[name] = (path, contact)
            pages[name] = page_one(contact)
        keys = stack.enter_context(extract_keys(process, pages, verify_key, require_all=False))
        matched = keys.aliases()
        if len(matched) != 1:
            raise ProbeError("account_missing_or_ambiguous")
        account, contact = contacts[matched[0]]
        with open_readonly(contact, keys.key(matched[0])) as conn:
            targets = resolve_targets(conn, config["private_hint"], config["group_hint"])
        directory = (account / "db_storage/message").resolve(strict=True)
        if not directory.is_relative_to(account):
            raise ProbeError("database_path_invalid")
        files = sorted(p for p in directory.iterdir() if re.fullmatch(r"message_[0-9]+\.db", p.name))
        if not 1 <= len(files) <= 16:
            raise ProbeError("database_count_unsupported")
        for path in files:
            if path.resolve() != path or not path.is_file():
                raise ProbeError("database_path_invalid")
        msg_pages = {f"shard_{i}": page_one(path) for i, path in enumerate(files)}
        # Fresh read budget; still attached to exactly the original client PID.
        msg_process = stack.enter_context(NativeProcess(client.pid))
        msg_keys = stack.enter_context(extract_keys(msg_process, msg_pages, verify_key))
        sources = []
        for i, path in enumerate(files):
            conn = stack.enter_context(open_readonly(path, msg_keys.key(f"shard_{i}")))
            sources.extend(make_sources(conn, path, targets))
        if any(not any(s.target.alias == t.alias for s in sources) for t in targets):
            raise ProbeError("target_message_table_missing")
        # Restart baselines together after the potentially slow discovery phase.
        for source in sources:
            source.baseline = source.connection.execute(
                f'SELECT COALESCE(MAX(local_id),0) FROM "{source.table}"'
            ).fetchone()[0]
        yield client, targets, sources


def run(config, *, duration=0, interval=1.0, prefix="XLD-00000000", emit=None):
    from .db_windows import window_state

    report = base_report()
    if type(duration) is not int or not 0 <= duration <= 900 or not 0.5 <= interval <= 5:
        raise ProbeError("configuration_invalid")
    tracker = MarkerTracker("", prefix)
    start_cpu = time.process_time()
    with prepare(config) as (client, targets, sources):
        tracker.owner_username = next(t.username for t in targets if t.alias == "owner_main")
        report.update(status="ready", client_version=client.version,
                      account_candidates=len(config["account_dirs"]), account_matches=1,
                      message_sources=len(sources), targets=tracker.targets, prefix=prefix,
                      polls=0, minimized_samples=0, foreground_target_samples=0, elapsed_seconds=0,
                      max_poll_seconds=0, cpu_seconds=round(time.process_time() - start_cpu, 2))
        if emit:
            emit(report)
        if duration == 0:
            return report
        start = time.monotonic()
        while time.monotonic() - start < duration:
            before = window_state(client.pid)
            poll_start = time.monotonic()
            batches = [(s, read_since_baseline(s)) for s in sources]
            after = window_state(client.pid)
            if before["window_count"] != 1 or after["window_count"] != 1:
                raise ProbeError("client_identity_changed")
            minimized = bool(before["all_minimized"] and after["all_minimized"])
            for source, rows in batches:
                for row in rows:
                    tracker.observe(str(source.path), source.target.alias, row, minimized)
            report["polls"] += 1
            report["minimized_samples"] += int(minimized)
            report["foreground_target_samples"] += int(before["foreground_is_target"] or
                                                       after["foreground_is_target"])
            report["max_poll_seconds"] = round(max(report["max_poll_seconds"],
                                                    time.monotonic() - poll_start), 4)
            report["elapsed_seconds"] = round(time.monotonic() - start, 2)
            report["cpu_seconds"] = round(time.process_time() - start_cpu, 2)
            report["status"] = "observing"
            if emit:
                emit(report)
            time.sleep(min(interval, max(0, duration - (time.monotonic() - start))))
        report["status"] = "watch_complete"
        report["elapsed_seconds"] = round(time.monotonic() - start, 2)
        report["cpu_seconds"] = round(time.process_time() - start_cpu, 2)
        if emit:
            emit(report)
        return report
