"""Receipt worker integration using synthetic SQLite, fake identity and clocks."""
from __future__ import annotations

import copy
import io
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from companion_wechat import db_receipt as dr
from companion_wechat import db_source, db_worker
from companion_wechat import send_receipt as proof
from companion_wechat.db_probe import Target, make_sources, message_table

SELF, OWNER, GROUP = "wxid_synthetic_self", "synthetic_owner", "synthetic_group@chatroom"
STAMP = 1700000000
TEXT = "synthetic receipt reply"


class Clock:
    value = 10.0

    def __call__(self):
        return self.value


class ReadSpy:
    def __init__(self, connection):
        self.connection = connection
        self.queries = []
        self.fail = ""

    def execute(self, sql, parameters=()):
        assert sql.split()[0].upper() in {"SELECT", "PRAGMA", "BEGIN", "ROLLBACK"}
        self.queries.append((sql, parameters))
        if self.fail and self.fail in sql:
            raise RuntimeError("private provider exception must never escape")
        return self.connection.execute(sql, parameters)


class Lab:
    def __init__(self, tmp_path):
        self.account = tmp_path / (SELF + "_1234")
        (self.account / "db_storage/message").mkdir(parents=True)
        self.client = SimpleNamespace(pid=42, executable="synthetic.exe", version="4.1.15.13")
        self.observed_client = self.client
        self.clock = Clock()
        self.targets = [Target("owner_main", OWNER), Target("family", GROUP)]
        self.raw, self.spies = {}, {}
        self.opens = self.closes = 0
        self.window_count = 1
        self.add_shard()

    def add_shard(self, name="message_0.db"):
        path = self.account / "db_storage/message" / name
        path.write_bytes(bytes([len(self.raw) + 1]) * 4096)
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
        connection.executemany("INSERT INTO Name2Id VALUES (?)", [(OWNER,), (SELF,), ("synthetic_member",)])
        for target in self.targets:
            connection.execute(f'CREATE TABLE "{message_table(target.username)}" '
                               '(local_id INTEGER PRIMARY KEY,server_id,create_time,local_type,'
                               'real_sender_id,message_content,sort_seq)')
        self.raw[name], self.spies[name] = connection, ReadSpy(connection)

    def add(self, local_id, *, shard="message_0.db", target=OWNER, sender=2,
            server_id=None, text=TEXT, created=STAMP, kind=1):
        self.raw[shard].execute(f'INSERT INTO "{message_table(target)}" VALUES (?,?,?,?,?,?,?)',
                               (local_id, 1000 + local_id if server_id is None else server_id,
                                created, kind, sender, text, 1))

    @contextmanager
    def prepare(self, config, enrollment):
        self.opens += 1
        sources = []
        for name, connection in self.spies.items():
            sources.extend(make_sources(connection, self.account / "db_storage/message" / name, self.targets))
        try:
            yield db_source.Prepared(self.client, self.account, SELF, self.targets, sources, tuple(self.raw))
        finally:
            self.closes += 1

    def session(self, config=None, enrollment=None, checkpoint=None):
        return db_source.SourceSession({}, enrollment, checkpoint, prepare_fn=self.prepare,
                                       clock=lambda: STAMP - 100,
                                       state_fn=lambda pid: {"window_count": self.window_count})

    def reader(self, session, target=OWNER):
        return dr.DatabaseReceiptReader(session, target, client_fn=lambda: self.observed_client,
                                        clock=self.clock)

    def handles(self, session):
        return dr.ReceiptHandles(session, client_fn=lambda: self.observed_client,
                                 clock=self.clock, wall_clock=lambda: STAMP)

    def begin(self, reader):
        return proof.begin_watermark(reader, reader.scope, clock=self.clock, wall_clock=lambda: STAMP)

    def check(self, reader, watermark, text=TEXT, **kwargs):
        return proof.verify_receipt(reader, watermark, text, clock=self.clock, **kwargs)


@pytest.fixture
def lab(tmp_path):
    lab = Lab(tmp_path)
    yield lab
    for connection in lab.raw.values():
        connection.close()


def test_live_heads_ignore_stale_receive_checkpoint_and_do_not_read_body(lab):
    with lab.session() as session:
        reader = lab.reader(session)
        checkpoint = copy.deepcopy(session.snapshot()["checkpoint"])
        lab.add(1, text="private old body", created=STAMP - 5)
        lab.spies["message_0.db"].queries.clear()
        watermark = lab.begin(reader)
        assert watermark.heads[0].anchor.local_id == 1
        assert not any("message_content" in sql for sql, _ in lab.spies["message_0.db"].queries)
        assert session.snapshot()["checkpoint"] == checkpoint
        assert not lab.check(reader, watermark).confirmed
        lab.add(2)
        assert lab.check(reader, watermark).confirmed
        assert session.snapshot()["checkpoint"] == checkpoint


def test_only_target_self_bodies_are_read_and_other_target_is_untouched(lab):
    with lab.session() as session:
        reader = lab.reader(session)
        watermark = lab.begin(reader)
        lab.add(1, sender=1, text="incoming private body")
        lab.add(2, target=GROUP, sender=3, text="other target private body")
        lab.add(3)
        lab.spies["message_0.db"].queries.clear()
        assert lab.check(reader, watermark).confirmed
        body_queries = [(sql, p) for sql, p in lab.spies["message_0.db"].queries if "message_content" in sql]
        assert len(body_queries) == 1
        assert body_queries[0][1] == (3, 1003)
        assert message_table(OWNER) in body_queries[0][0]
        assert "length(CAST(message_content AS BLOB))<=4096" in body_queries[0][0]


def test_group_sender_prefix_is_removed_only_for_verified_self(lab):
    with lab.session() as session:
        reader = lab.reader(session, GROUP)
        watermark = lab.begin(reader)
        lab.add(1, target=GROUP, sender=3, text="synthetic_member:\n" + TEXT)
        lab.add(2, target=GROUP, text=SELF + ":\n" + TEXT)
        assert lab.check(reader, watermark).confirmed


@pytest.mark.parametrize("fault", ["head-server", "middle-server", "sender", "type", "created", "text"])
def test_pending_rows_cannot_be_skipped_to_confirm_later_echo(lab, fault):
    with lab.session() as session:
        handles = lab.handles(session)
        handle = handles.begin("action", OWNER)["handle"]
        kwargs = {"server_id": 0} if fault in {"head-server", "middle-server"} else (
            {"sender": 999} if fault == "sender" else {"kind": 0} if fault == "type" else
            {"created": 0} if fault == "created" else {"text": b"\xff"})
        lab.add(1, **kwargs)
        if fault != "head-server":
            lab.add(2)
        assert handles.check("action", handle, TEXT) == {"status": "pending", "confirmed": False}
        lab.raw["message_0.db"].execute(f'UPDATE "{message_table(OWNER)}" '
                                      'SET server_id=1001,real_sender_id=2,local_type=1,create_time=?,message_content=? '
                                      'WHERE local_id=1', (STAMP, "different text" if fault != "head-server" else TEXT))
        assert handles.check("action", handle, TEXT) == {"status": "client_recorded", "confirmed": True}


@pytest.mark.parametrize("payload", ["private\0" + "x" * 5000, "界" * 1400,
                                     b"\x28\xb5\x2f\xfd" + bytes(5000)])
def test_oversized_self_body_never_reaches_decoder(lab, monkeypatch, payload):
    seen = []
    original = dr.short_text
    monkeypatch.setattr(dr, "short_text", lambda value: seen.append(value) or original(value))
    with lab.session() as session:
        reader = lab.reader(session)
        watermark = lab.begin(reader)
        lab.add(1, text=payload)
        assert not lab.check(reader, watermark).confirmed
        assert seen == [None]


def test_zstd_short_self_body_is_decoded_with_existing_bounded_decoder(lab):
    zstandard = pytest.importorskip("zstandard")

    with lab.session() as session:
        reader = lab.reader(session)
        watermark = lab.begin(reader)
        lab.add(1, text=zstandard.ZstdCompressor().compress(TEXT.encode()))
        assert lab.check(reader, watermark).confirmed


def test_history_server_id_membership_checks_all_original_target_shards(lab):
    lab.add_shard("message_1.db")
    lab.add(1, shard="message_1.db", server_id=31, created=STAMP - 3)
    lab.add(2, shard="message_1.db", server_id=32, created=STAMP - 2)
    with lab.session() as session:
        reader = lab.reader(session)
        watermark = lab.begin(reader)
        lab.add(3, server_id=31)
        assert lab.check(reader, watermark).reason == "receipt_server_id_reused"
        historical = [sql for spy in lab.spies.values() for sql, _ in spy.queries
                      if "DISTINCT server_id" in sql]
        assert historical and all("message_content" not in sql for sql in historical)


@pytest.mark.parametrize("fault", ["missing-anchor", "changed-anchor", "salt", "inode", "shard-added",
                                   "table-missing", "source-table", "pid", "version", "closed-window"])
def test_source_change_after_begin_never_confirms(lab, fault):
    lab.add(1, created=STAMP - 1)
    with lab.session() as session:
        reader = lab.reader(session)
        watermark = lab.begin(reader)
        lab.add(2)
        if fault == "missing-anchor":
            lab.raw["message_0.db"].execute(f'DELETE FROM "{message_table(OWNER)}" WHERE local_id=1')
        elif fault == "changed-anchor":
            lab.raw["message_0.db"].execute(f'UPDATE "{message_table(OWNER)}" SET server_id=42 WHERE local_id=1')
        elif fault == "salt":
            (lab.account / "db_storage/message/message_0.db").write_bytes(b"X" * 4096)
        elif fault == "inode":
            next(iter(session._sources.values())).inode = -1
        elif fault == "shard-added":
            lab.add_shard("message_1.db")
        elif fault == "table-missing":
            lab.raw["message_0.db"].execute(f'DROP TABLE "{message_table(OWNER)}"')
        elif fault == "source-table":
            next(iter(session._sources.values())).table = message_table(GROUP)
        elif fault in {"pid", "version"}:
            lab.observed_client = SimpleNamespace(**vars(lab.client))
            setattr(lab.observed_client, fault, 43 if fault == "pid" else "unexpected")
        else:
            lab.window_count = 0
        assert not lab.check(reader, watermark).confirmed


def test_unexposed_prepared_shard_is_not_silently_ignored(lab):
    lab.add_shard("message_1.db")
    for target in lab.targets:
        lab.raw["message_1.db"].execute(f'DROP TABLE "{message_table(target.username)}"')
    with lab.session() as session:
        with pytest.raises(db_source.SourceError, match="source_catalog_changed"):
            lab.reader(session)


def test_new_authorized_table_in_existing_shard_invalidates_catalog(lab):
    lab.raw["message_0.db"].execute(f'DROP TABLE "{message_table(GROUP)}"')
    lab.add_shard("message_1.db")
    with lab.session() as session:
        reader = lab.reader(session)
        watermark = lab.begin(reader)
        lab.raw["message_0.db"].execute(f'CREATE TABLE "{message_table(GROUP)}" (local_id)')
        lab.add(1)
        assert not lab.check(reader, watermark).confirmed


def test_metadata_scans_and_plaintext_reads_are_bounded(lab):
    lab.add_shard("message_1.db")
    with lab.session() as session:
        reader = lab.reader(session)
        watermark = lab.begin(reader)
        for i in range(1, 301):
            lab.add(i)
            lab.add(i, shard="message_1.db", server_id=10000 + i)
        for spy in lab.spies.values():
            spy.queries.clear()
        report = reader.read_self_echo(reader.scope, watermark.heads, limit=100, scan_limit=512)
        assert report["scanned"] == 512
        assert len(report["rows"]) == 100
        assert report["complete"] is False
        body_reads = [sql for spy in lab.spies.values() for sql, _ in spy.queries if "message_content" in sql]
        assert len(body_reads) == 100
        for spy in lab.spies.values():
            metadata = [(sql, p) for sql, p in spy.queries if "LEFT JOIN Name2Id" in sql]
            assert len(metadata) == 1 and metadata[0][1][-1] == 256


def test_ambiguous_same_text_and_transaction_rollback(lab):
    with lab.session() as session:
        reader = lab.reader(session)
        watermark = lab.begin(reader)
        lab.add(1)
        lab.add(2)
        assert lab.check(reader, watermark).status == "ambiguous"
        assert all(not c.in_transaction for c in lab.raw.values())


def test_sql_failure_rolls_back_all_sources_and_redacts_error(lab):
    lab.add_shard("message_1.db")
    with lab.session() as session:
        reader = lab.reader(session)
        watermark = lab.begin(reader)
        lab.add(1)
        lab.spies["message_1.db"].fail = "ORDER BY local_id DESC"
        result = lab.check(reader, watermark)
        assert result.reason == "receipt_reader_failed"
        assert "private" not in repr(result)
        assert all(not c.in_transaction for c in lab.raw.values())


def test_existing_readonly_sqlite_authorizer_accepts_receipt_queries(lab):
    from companion_wechat.db_sqlite import ReadOnlyConnection

    # Shared synthetic memory DB: the test writer is distinct from the reader
    # whose exact production SQL authorizer/timeout facade is under test.
    uri = f"file:receipt_authorizer_{id(lab)}?mode=memory&cache=shared"
    writer = sqlite3.connect(uri, uri=True, isolation_level=None)
    lab.raw["message_0.db"].backup(writer)
    lab.raw["message_0.db"].close()
    lab.raw["message_0.db"] = writer
    native_reader = sqlite3.connect(uri, uri=True, isolation_level=None)
    facade = ReadOnlyConnection(native_reader, sqlite3, query_timeout=1.0)
    lab.spies["message_0.db"] = ReadSpy(facade)
    try:
        lab.add(1, created=STAMP - 1)
        with lab.session() as session:
            reader = lab.reader(session)
            watermark = lab.begin(reader)
            lab.add(2)
            assert lab.check(reader, watermark).confirmed
            assert any("DISTINCT server_id" in sql for sql, _ in lab.spies["message_0.db"].queries)
    finally:
        facade.close()


def test_source_connection_replacement_invalidates_receipt(lab):
    with lab.session() as session:
        reader = lab.reader(session)
        watermark = lab.begin(reader)
        lab.add(1)
        source = next(iter(session._sources.values()))
        original = source.connection
        source.connection = ReadSpy(lab.raw["message_0.db"])
        try:
            assert not lab.check(reader, watermark).confirmed
        finally:
            source.connection = original


def test_default_receipt_discovery_pins_authenticated_pid_when_another_client_is_running(lab, monkeypatch):
    from companion_wechat import db_windows

    selected = []

    def discover(*, pid=None):
        selected.append(pid)
        if pid is None:
            raise db_windows.WindowsProbeError("client_ambiguous")
        assert pid == lab.client.pid
        return lab.client

    monkeypatch.setattr(db_windows, "discover_wechat", discover)
    with lab.session() as session:
        reader = dr.DatabaseReceiptReader(session, OWNER, clock=lab.clock)
        watermark = lab.begin(reader)
        lab.add(1)
        assert lab.check(reader, watermark).confirmed
    assert selected and set(selected) == {lab.client.pid}


def test_ram_handle_single_use_expiration_and_restart_invalidation(lab):
    with lab.session() as session:
        handles = lab.handles(session)
        first = handles.begin("first", OWNER)
        assert set(first) == {"handle"} and len(first["handle"]) == 64
        lab.add(1)
        assert handles.check("first", first["handle"], TEXT) == {"status": "client_recorded", "confirmed": True}
        assert handles.check("first", first["handle"], TEXT) == {"status": "uncertain", "confirmed": False}
        second = handles.begin("second", OWNER)["handle"]
        lab.clock.value += 121
        assert handles.check("second", second, TEXT) == {"status": "uncertain", "confirmed": False}
        third = handles.begin("third", OWNER)["handle"]
        fresh = lab.handles(session)
        assert fresh.check("third", third, TEXT) == {"status": "uncertain", "confirmed": False}
        handles.close()
        assert handles._entries == {}


def test_one_active_action_per_target_and_discard_is_idempotent(lab):
    with lab.session() as session:
        handles = lab.handles(session)
        handle = handles.begin("first", OWNER)["handle"]
        with pytest.raises(dr.DatabaseReceiptError, match="receipt_action_busy"):
            handles.begin("second", OWNER)
        with pytest.raises(dr.DatabaseReceiptError, match="receipt_action_busy"):
            handles.begin("first", GROUP)
        assert handles.begin("second", GROUP)["handle"] != handle
        assert handles.discard("first", handle) == {"discarded": True}
        assert handles.discard("first", handle) == {"discarded": True}
        assert handles.begin("third", OWNER)


def test_handle_binding_cannot_be_switched_to_another_action_or_text(lab):
    with lab.session() as session:
        handles = lab.handles(session)
        handle = handles.begin("first", OWNER)["handle"]
        with pytest.raises(dr.DatabaseReceiptError):
            handles.check("different", handle, TEXT)
        with pytest.raises(dr.DatabaseReceiptError):
            handles.discard("different", handle)
        assert handles.check("first", handle, TEXT)["status"] == "pending"
        lab.add(1, text="changed body")
        assert handles.check("first", handle, "changed body") == {"status": "uncertain", "confirmed": False}
        assert handle not in handles._entries


def test_only_enrolled_raw_targets_are_accepted(lab):
    with lab.session() as session:
        handles = lab.handles(session)
        for target in ("owner_main", "family", "display name", SELF, "unknown", None):
            with pytest.raises(dr.DatabaseReceiptError):
                handles.begin("first", target)
        assert handles._entries == {}


class ScriptInput:
    def __init__(self, steps):
        self.steps = iter(steps)

    def readline(self, _size):
        try:
            return json.dumps(next(self.steps)) + "\n"
        except StopIteration:
            return ""


def test_private_worker_receipt_protocol_and_original_commands(lab):
    output = io.StringIO()
    managers = []

    def factory(session):
        manager = lab.handles(session)
        managers.append(manager)
        return manager

    def script():
        yield {"id": 1, "command": "open", "config": {}, "enrollment": None, "checkpoint": None}
        checkpoint = json.loads(output.getvalue().splitlines()[-1])["result"]["checkpoint"]
        yield {"id": 2, "command": "receipt_begin", "action_id": "action", "target_ref": OWNER}
        handle = json.loads(output.getvalue().splitlines()[-1])["result"]["handle"]
        lab.add(1)
        yield {"id": 3, "command": "receipt_check", "action_id": "action", "handle": handle, "text": TEXT}
        yield {"id": 4, "command": "snapshot"}
        assert json.loads(output.getvalue().splitlines()[-1])["result"]["checkpoint"] == checkpoint
        yield {"id": 5, "command": "receipt_discard", "action_id": "action", "handle": handle}
        yield {"id": 6, "command": "close"}

    assert db_worker.serve(ScriptInput(script()), output, session_factory=lab.session,
                           receipt_factory=factory) == 0
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[2] == {"id": 3, "ok": True, "result": {"status": "client_recorded", "confirmed": True}}
    for reply in (replies[1], replies[2], replies[4]):
        assert TEXT not in json.dumps(reply)
        assert OWNER not in json.dumps(reply)
        assert SELF not in json.dumps(reply)
        assert "server_id" not in json.dumps(reply)
    assert managers[0]._closed and managers[0]._entries == {}
    assert lab.opens == lab.closes == 1


@pytest.mark.parametrize("stop", ["eof", "invalid-command", "reader-failure"])
def test_worker_cleans_receipt_handles_and_source_connections(lab, stop):
    output, managers = io.StringIO(), []

    def factory(session):
        manager = lab.handles(session)
        managers.append(manager)
        return manager

    def script():
        yield {"id": 1, "command": "open", "config": {}, "enrollment": None, "checkpoint": None}
        yield {"id": 2, "command": "receipt_begin", "action_id": "action", "target_ref": OWNER}
        if stop == "invalid-command":
            yield {"id": 3, "command": "receipt_begin", "action_id": "action", "target_ref": "secret", "extra": 1}
        elif stop == "reader-failure":
            lab.spies["message_0.db"].fail = "ORDER BY local_id DESC"
            yield {"id": 3, "command": "receipt_begin", "action_id": "second", "target_ref": GROUP}

    result = db_worker.serve(ScriptInput(script()), output, session_factory=lab.session, receipt_factory=factory)
    assert result == (0 if stop == "eof" else 2)
    assert managers[0]._closed and managers[0]._entries == {}
    assert lab.opens == lab.closes == 1
    assert "private provider" not in output.getvalue()


@pytest.mark.parametrize("command,fields", [("receipt_begin", {"action_id": "x", "target_ref": OWNER}),
                                          ("receipt_check", {"action_id": "x", "handle": "a" * 64, "text": TEXT}),
                                          ("receipt_discard", {"action_id": "x", "handle": "a" * 64})])
def test_receipt_commands_require_open_worker(command, fields):
    output = io.StringIO()
    request = dict(fields, id=1, command=command)
    assert db_worker.serve(io.StringIO(json.dumps(request) + "\n"), output,
                           session_factory=lambda *_: pytest.fail("no real session")) == 2
    assert json.loads(output.getvalue())["error"] == "source_closed"
