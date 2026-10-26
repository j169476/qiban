"""Production-source semantics using synthetic SQLite and fake preparation."""
from __future__ import annotations

import copy
import io
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from companion_wechat import db_source as ds
from companion_wechat import db_worker
from companion_wechat.db_probe import Target, make_sources, message_table

OWNER = "synthetic_owner"
SELF = "wxid_synthetic_bot"
MEMBER = "synthetic_member"
GROUP = "synthetic_group@chatroom"
SINCE = 1700000000


class Harness:
    def __init__(self, tmp_path):
        self.account = tmp_path / (SELF + "_0001")
        (self.account / "db_storage/message").mkdir(parents=True)
        self.targets = [Target("owner_main", OWNER), Target("family", GROUP)]
        self.own = SELF
        self.shards = {}
        self.opens = self.closes = 0
        self.add_shard()

    def add_shard(self, name="message_0.db"):
        path = self.account / "db_storage/message" / name
        path.write_bytes(bytes([len(self.shards) + 1]) * 16 + bytes(4080))
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
        conn.executemany("INSERT INTO Name2Id VALUES (?)", [(OWNER,), (SELF,), (MEMBER,)])
        for target in self.targets:
            table = message_table(target.username)
            conn.execute(f'CREATE TABLE "{table}" (local_id INTEGER PRIMARY KEY, server_id, create_time,'
                         'local_type, real_sender_id, message_content, sort_seq)')
        self.shards[name] = conn
        return conn

    def insert(self, local_id, *, name="message_0.db", target=OWNER, server_id=None,
               created=SINCE + 1, kind=1, sender=1, text="synthetic new message"):
        self.shards[name].execute(f'INSERT INTO "{message_table(target)}" VALUES (?,?,?,?,?,?,?)',
                                 (local_id, server_id if server_id is not None else 1000 + local_id,
                                  created, kind, sender, text, 9))

    @contextmanager
    def prepare(self, config, enrollment):
        self.opens += 1
        sources = []
        for name, conn in sorted(self.shards.items()):
            path = self.account / "db_storage/message" / name
            sources.extend(make_sources(conn, path, self.targets))
        try:
            yield ds.Prepared(SimpleNamespace(pid=1), self.account, self.own, self.targets,
                              sources, tuple(sorted(self.shards)))
        finally:
            self.closes += 1

    def session(self, enrollment=None, checkpoint=None):
        return ds.SourceSession({}, enrollment, checkpoint, prepare_fn=self.prepare, clock=lambda: SINCE,
                                state_fn=lambda _: {"window_count": 1})

    def close(self):
        for conn in self.shards.values():
            conn.close()


@pytest.fixture
def lab(tmp_path):
    lab = Harness(tmp_path)
    yield lab
    lab.close()


def test_enrollment_reads_no_history_body_and_resume_does_not_rebaseline(lab):
    lab.insert(1, created=SINCE - 100, text="PRIVATE-HISTORICAL-BODY")
    with lab.session() as first:
        snapshot = first.snapshot()
        assert "sources" not in snapshot["enrollment"]
        assert "PRIVATE" not in json.dumps(snapshot)
        assert first.read(snapshot["checkpoint"])["rows"] == []
    lab.insert(2, text="arrived while worker disconnected")
    with lab.session(**snapshot) as resumed:
        result = resumed.read(snapshot["checkpoint"])
        assert [row["text"] for row in result["rows"]] == ["arrived while worker disconnected"]
        assert resumed.snapshot()["checkpoint"] == snapshot["checkpoint"]
    assert lab.opens == lab.closes == 2


def test_same_checkpoint_replays_identical_batch_until_caller_ack(lab):
    with lab.session() as session:
        baseline = session.snapshot()["checkpoint"]
        lab.insert(1)
        first = session.read(baseline)
        second = session.read(baseline)
        assert first == second
        assert session.read(first["next_checkpoint"])["rows"] == []
        assert all(item["anchor"] is None for item in baseline["sources"].values())


def test_bounded_pages_preserve_equal_sort_sequences_and_identical_text(lab):
    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        for i in range(1, 106):
            lab.insert(i, text="same body")
        first = session.read(checkpoint)
        second = session.read(first["next_checkpoint"])
        third = session.read(second["next_checkpoint"])
        assert [len(batch["rows"]) for batch in (first, second, third)] == [50, 50, 5]
        assert len({row["server_id"] for batch in (first, second, third) for row in batch["rows"]}) == 105


@pytest.mark.parametrize("fault,reason", [("server", "pending_server_id"),
                                         ("sender", "pending_sender"),
                                         ("time", "pending_create_time"),
                                         ("type", "pending_message_type"),
                                         ("text", "text_unavailable")])
def test_pending_or_invalid_text_blocks_without_skipping_later_rows(lab, fault, reason):
    kwargs = {"server_id": 0} if fault == "server" else {"sender": 999} if fault == "sender" else (
        {"created": 0} if fault == "time" else {"kind": 0} if fault == "type" else {"text": b"\xff"})
    with lab.session() as session:
        initial = session.snapshot()["checkpoint"]
        lab.insert(1, **kwargs)
        lab.insert(2)
        blocked = session.read(initial)
        assert blocked["rows"] == []
        assert blocked["next_checkpoint"] == initial
        assert blocked["blocked"] == [{"source": "message/message_0.db|owner_main", "reason": reason}]
        lab.shards["message_0.db"].execute(
            f'UPDATE "{message_table(OWNER)}" SET server_id=1001,real_sender_id=1,'
            'create_time=?,local_type=1,message_content=? WHERE local_id=1', (SINCE + 1, "fixed"))
        recovered = session.read(initial)
        assert [r["server_id"] for r in recovered["rows"]] == ["1001", "1002"]


@pytest.mark.parametrize("storage", ["nul-text", "multibyte-text", "compressed-blob", "compressed-text"])
def test_oversized_byte_payload_is_not_returned_to_decoder_or_skipped(lab, monkeypatch, storage):
    # SQLite length(TEXT) counts characters and stops at NUL. WCDB can also
    # label compressed binary data as TEXT. Neither is a byte-size bound.
    if storage == "nul-text":
        payload = "PRIVATE-OVERSIZE\0" + "x" * 5000
    elif storage == "multibyte-text":
        payload = "界" * 1400
    else:
        payload = b"\x28\xb5\x2f\xfd" + b"\0" * 5000
    observed = []
    original_decoder = ds.short_text

    def bounded_decoder(value):
        observed.append(value)
        assert value is None, "SQL must return NULL instead of oversized message content"
        return original_decoder(value)

    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        lab.insert(1, text=payload)
        lab.insert(2, text="next message must wait")
        if storage == "compressed-text":
            lab.shards["message_0.db"].execute(
                f'UPDATE "{message_table(OWNER)}" SET message_content=CAST(? AS TEXT) WHERE local_id=1',
                (payload,),
            )
        monkeypatch.setattr(ds, "short_text", bounded_decoder)
        blocked = session.read(checkpoint)
        assert observed == [None]
        assert blocked["rows"] == []
        assert blocked["next_checkpoint"] == checkpoint
        assert blocked["blocked"] == [{"source": "message/message_0.db|owner_main",
                                        "reason": "text_unavailable"}]
        assert "PRIVATE-OVERSIZE" not in json.dumps(blocked)
        monkeypatch.setattr(ds, "short_text", original_decoder)
        lab.shards["message_0.db"].execute(
            f'UPDATE "{message_table(OWNER)}" SET message_content=? WHERE local_id=1', ("bounded text",))
        recovered = session.read(checkpoint)
        assert [row["server_id"] for row in recovered["rows"]] == ["1001", "1002"]


def test_one_pending_conversation_does_not_block_other_target(lab):
    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        lab.insert(1, server_id=0)
        lab.insert(1, target=GROUP, sender=3, text=MEMBER + ":\nhello")
        result = session.read(checkpoint)
        assert result["rows"] == [{"target_ref": GROUP, "kind": "group", "server_id": "1001",
                                   "sender_ref": MEMBER, "text": "hello",
                                   "occurred_at": "2023-11-14T22:13:21+00:00", "direction": "incoming",
                                   "trigger": "none", "time_basis": "server"}]


def test_busy_private_source_cannot_starve_family(lab):
    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        for i in range(1, 151):
            lab.insert(i)
        lab.insert(1, target=GROUP, sender=3, text="family must get a slot")
        batch = session.read(checkpoint)
        assert len(batch["rows"]) == 51
        assert any(row["target_ref"] == GROUP for row in batch["rows"])


def test_client_exit_requires_reconnect_instead_of_reading_cached_old_session(lab):
    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        session._state = lambda _: {"window_count": 0}
        with pytest.raises(ds.SourceError, match="client_not_running"):
            session.read(checkpoint)


def test_smaller_pages_rotate_sources_without_starvation(lab):
    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        lab.insert(1)
        lab.insert(1, target=GROUP, sender=3)
        first = session.read(checkpoint, limit=1)
        second = session.read(first["next_checkpoint"], limit=1)
        assert len(first["rows"]) == len(second["rows"]) == 1
        assert {first["rows"][0]["target_ref"], second["rows"][0]["target_ref"]} == {OWNER, GROUP}


def test_more_than_one_page_of_targets_has_durable_fair_progress(lab):
    extras = [Target(f"target_{i}", f"{10000+i}@chatroom") for i in range(105)]
    lab.targets.extend(extras)
    lab.add_shard("message_1.db")
    with lab.session() as session:
        snapshot = session.snapshot()
        assert len(snapshot["enrollment"]["targets"]) == 107
        for target in extras:
            lab.insert(1, name="message_1.db", target=target.username, sender=3)
        first = session.read(snapshot["checkpoint"])
        # Replaying an unacknowledged page is deterministic, including its rotation pointer.
        assert session.read(snapshot["checkpoint"]) == first
        checkpoint = first["next_checkpoint"]
    with lab.session(snapshot["enrollment"], checkpoint) as resumed:
        second = resumed.read(checkpoint)
        identifiers = [r["target_ref"] for r in first["rows"] + second["rows"]]
        assert set(identifiers) == {t.username for t in extras}
        assert len(identifiers) == len(set(identifiers))


def test_self_rows_are_explicitly_identified_and_private_stranger_blocks(lab):
    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        lab.insert(1, sender=2)
        lab.insert(2, sender=3)
        result = session.read(checkpoint)
        assert result["rows"][0]["direction"] == "self"
        assert result["rows"][0]["sender_ref"] == SELF
        assert result["blocked"][0]["reason"] == "private_sender_mismatch"
        assert result["next_checkpoint"]["sources"]["message/message_0.db|owner_main"]["anchor"]["local_id"] == 1


def test_nontext_can_advance_but_no_message_body_is_exported(lab):
    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        lab.insert(1, kind=3, text="PRIVATE-IMAGE-XML")
        lab.insert(2)
        result = session.read(checkpoint)
        assert result["skipped"] == 1
        assert len(result["rows"]) == 1 and result["rows"][0]["server_id"] == "1002"
        assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["delete", "server", "time"])
def test_committed_anchor_missing_or_changed_fails_closed(lab, mutation):
    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        lab.insert(1)
        checkpoint = session.read(checkpoint)["next_checkpoint"]
        conn, table = lab.shards["message_0.db"], message_table(OWNER)
        if mutation == "delete":
            conn.execute(f'DELETE FROM "{table}"')
        elif mutation == "server":
            conn.execute(f'UPDATE "{table}" SET server_id=9999')
        else:
            conn.execute(f'UPDATE "{table}" SET create_time=create_time+1')
        with pytest.raises(ds.SourceError, match="source_anchor_changed"):
            session.read(checkpoint)
        assert not conn.in_transaction


def test_reconnect_anchor_changes_are_not_replaced_with_current_max(lab):
    lab.insert(1)
    with lab.session() as session:
        snapshot = session.snapshot()
    lab.shards["message_0.db"].execute(f'UPDATE "{message_table(OWNER)}" SET server_id=9999')
    with pytest.raises(ds.SourceError, match="source_anchor_changed"):
        with lab.session(**snapshot):
            pass
    assert lab.opens == lab.closes


def test_new_shard_requires_reopen_and_filters_history_without_decoding_it(lab):
    with lab.session() as session:
        snapshot = session.snapshot()
        lab.add_shard("message_1.db")
        lab.insert(1, name="message_1.db", created=SINCE - 10, text=b"\xff historical")
        lab.insert(2, name="message_1.db", text="new shard live row")
        with pytest.raises(ds.SourceError, match="source_catalog_changed"):
            session.read(snapshot["checkpoint"])
    with lab.session(**snapshot) as resumed:
        batch = resumed.read(snapshot["checkpoint"])
        assert [r["text"] for r in batch["rows"]] == ["new shard live row"]
        assert batch["skipped"] == 1
        assert resumed.read(snapshot["checkpoint"])["rows"] == batch["rows"]
        assert resumed.read(batch["next_checkpoint"])["rows"] == []


def test_original_shard_loss_cannot_silently_create_new_baseline(lab):
    lab.add_shard("message_1.db")
    with lab.session() as session:
        snapshot = session.snapshot()
    removed = lab.shards.pop("message_0.db")
    removed.close()
    (lab.account / "db_storage/message/message_0.db").unlink()
    with pytest.raises(ds.SourceError, match="source_generation_changed"):
        with lab.session(**snapshot):
            pass


def test_salt_change_rejects_same_path_and_same_row_ids(lab):
    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        (lab.account / "db_storage/message/message_0.db").write_bytes(b"N" * 16 + bytes(4080))
        with pytest.raises(ds.SourceError, match="source_generation_changed"):
            session.read(checkpoint)


@pytest.mark.parametrize("changed", ["self", "target", "directory"])
def test_resume_never_follows_another_account_or_target(lab, changed):
    with lab.session() as session:
        snapshot = session.snapshot()
    if changed == "self":
        lab.own = "different_self"
    elif changed == "target":
        lab.targets[0] = Target("owner_main", "different_owner")
    else:
        snapshot["enrollment"]["account_directory_id"] = "0" * 64
    with pytest.raises(ds.SourceError, match="source_identity_changed"):
        with lab.session(**snapshot):
            pass


def test_close_disposes_preparation_and_disallows_more_reads(lab):
    with lab.session() as session:
        snapshot = session.snapshot()
        assert "synthetic" not in repr(session)
    assert lab.closes == 1
    with pytest.raises(ds.SourceError, match="source_closed"):
        session.read(snapshot["checkpoint"])


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_read_limit_is_explicitly_bounded(lab, limit):
    with lab.session() as session:
        with pytest.raises(ds.SourceError, match="source_configuration_invalid"):
            session.read(session.snapshot()["checkpoint"], limit=limit)


@pytest.mark.parametrize("fault", ["epoch", "missing_source", "bad_salt", "negative_anchor"])
def test_checkpoint_validation_rejects_corruption_without_advancing(lab, fault):
    with lab.session() as session:
        snapshot = session.snapshot()
        checkpoint = copy.deepcopy(snapshot["checkpoint"])
        key = next(iter(checkpoint["sources"]))
        if fault == "epoch":
            checkpoint["epoch"] = "0" * 32
        elif fault == "missing_source":
            del checkpoint["sources"][key]
        elif fault == "bad_salt":
            checkpoint["sources"][key]["salt"] = "PRIVATE"
        else:
            checkpoint["sources"][key]["anchor"] = {"local_id": -1, "server_id": 1, "create_time": 1}
        with pytest.raises(ds.SourceError, match="source_checkpoint_invalid"):
            session.read(checkpoint)
        assert session.snapshot() == snapshot


def test_self_identity_requires_exact_unique_contact_match():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE contact(username TEXT COLLATE NOCASE,alias TEXT COLLATE NOCASE)")
        conn.execute("INSERT INTO contact VALUES (?,?)", (SELF, "custom_self"))
        assert ds.self_username(conn, SELF + "_a1b2") == SELF
        assert ds.self_username(conn, "custom_self_a1b2") == SELF
        with pytest.raises(ds.SourceError, match="self_identity_unverified"):
            ds.self_username(conn, SELF.upper() + "_a1b2")
        with pytest.raises(ds.SourceError, match="self_identity_unverified"):
            ds.self_username(conn, "missing_suffix")
        conn.execute("INSERT INTO contact VALUES (?,?)", ("other_user", "custom_self"))
        with pytest.raises(ds.SourceError, match="self_identity_unverified"):
            ds.self_username(conn, "custom_self_a1b2")
    finally:
        conn.close()


@pytest.fixture
def native_boundary(lab, tmp_path, monkeypatch):
    """Exercise native preparation orchestration with every native operation fake."""
    from companion_wechat import db_sqlite, db_windows

    with lab.session() as session:
        enrollment = session.snapshot()["enrollment"]
    other = tmp_path / "synthetic_other_0002"
    other.mkdir()
    config = {"account_dirs": [str(other), str(lab.account)], "private_hint": OWNER,
              "group_hint": "synthetic group", "client_pid": 77}
    contact = sqlite3.connect(":memory:")
    contact.execute("CREATE TABLE contact(username TEXT,alias TEXT)")
    contact.executemany("INSERT INTO contact VALUES (?,?)", [(SELF, ""), (OWNER, ""),
                                                            ("synthetic_group@chatroom", "")])
    calls = {"discover": [], "process": [], "pages": [], "extract": [], "open": []}
    state = {"no_keys": False}

    def discover(*, pid=None):
        calls["discover"].append(pid)
        return SimpleNamespace(pid=pid, executable="synthetic.exe", version="4.1.15.13")

    class Process:
        def __init__(self, pid):
            self.pid = pid
            calls["process"].append(pid)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    class Keys:
        def __init__(self, aliases):
            self._aliases = aliases

        def aliases(self):
            return list(self._aliases)

        def key(self, alias):
            assert alias in self._aliases
            return bytes(32)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def extract(process, pages, verify, **kwargs):
        calls["extract"].append((process.pid, tuple(pages)))
        return Keys([] if state["no_keys"] else list(pages))

    def page(path):
        calls["pages"].append(path)
        return bytes(4096)

    @contextmanager
    def open_readonly(path, key):
        calls["open"].append(path)
        yield contact if path.name == "contact.db" else lab.shards[path.name]

    monkeypatch.setattr(db_windows, "discover_wechat", discover)
    monkeypatch.setattr(db_windows, "NativeProcess", Process)
    monkeypatch.setattr(db_windows, "extract_keys", extract)
    monkeypatch.setattr(db_sqlite, "open_readonly", open_readonly)
    monkeypatch.setattr(ds, "safe_db_path", lambda account, rel: account / "db_storage" / rel)
    monkeypatch.setattr(ds, "page_one", page)
    yield config, enrollment, calls, contact, state
    contact.close()


def test_native_resume_pins_pid_and_only_authenticates_enrolled_account(native_boundary, lab):
    config, enrollment, calls, _, _ = native_boundary
    with ds._native_prepare(config, enrollment) as prepared:
        assert prepared.account == lab.account
        assert prepared.self_username == SELF
    assert calls["discover"] == [77]
    assert calls["process"] == [77, 77]
    assert calls["extract"][0] == (77, ("candidate_0",))
    assert calls["pages"] and all(path.is_relative_to(lab.account) for path in calls["pages"])
    assert calls["open"] and all(path.is_relative_to(lab.account) for path in calls["open"])


def test_native_resume_wrong_account_directory_fails_before_process_or_page_access(native_boundary):
    config, enrollment, calls, _, _ = native_boundary
    enrollment["account_directory_id"] = "0" * 64
    with pytest.raises(ds.SourceError, match="source_identity_changed"):
        with ds._native_prepare(config, enrollment):
            pytest.fail("must not prepare another account")
    assert all(not values for values in calls.values())


def test_native_resume_wrong_pid_does_not_try_other_processes_or_accounts(native_boundary):
    config, enrollment, calls, _, state = native_boundary
    config["client_pid"] = 88
    state["no_keys"] = True
    with pytest.raises(ds.SourceError, match="key_not_found"):
        with ds._native_prepare(config, enrollment):
            pytest.fail("wrong pinned process must fail closed")
    assert calls["discover"] == [88]
    assert calls["process"] == [88]
    assert calls["open"] == []
    assert calls["extract"] == [(88, ("candidate_0",))]


def test_native_resume_rejects_wrong_self_before_message_key_or_shard_reads(native_boundary):
    config, enrollment, calls, contact, _ = native_boundary
    contact.execute("UPDATE contact SET username=?,alias=? WHERE username=?", ("wrong_self", SELF, SELF))
    with pytest.raises(ds.SourceError, match="source_identity_changed"):
        with ds._native_prepare(config, enrollment):
            pytest.fail("wrong self must not open message shards")
    assert calls["process"] == [77]
    assert len(calls["extract"]) == 1
    assert [path.name for path in calls["pages"]] == ["contact.db"]
    assert [path.name for path in calls["open"]] == ["contact.db"]


def test_worker_protocol_is_private_bounded_and_closes_session_at_eof(lab):
    commands = [{"id": 1, "command": "open", "config": {}, "enrollment": None, "checkpoint": None},
                {"id": 2, "command": "snapshot"}, {"id": 3, "command": "close"}]
    output = io.StringIO()
    result = db_worker.serve(io.StringIO("".join(json.dumps(c) + "\n" for c in commands)), output,
                             session_factory=lambda *_: lab.session())
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert result == 0 and all(reply["ok"] for reply in replies)
    assert replies[0]["result"] == replies[1]["result"]
    assert replies[2]["result"] == {"closed": True}
    assert lab.opens == lab.closes == 1


def test_worker_failure_closes_keys_and_redacts_arbitrary_exception():
    closed = []

    class Broken:
        def __init__(self, *_):
            pass

        def __enter__(self):
            raise RuntimeError("PRIVATE-KEY-PATH")

        def close(self):
            closed.append(True)

    request = {"id": 1, "command": "open", "config": {}, "enrollment": None, "checkpoint": None}
    output = io.StringIO()
    assert db_worker.serve(io.StringIO(json.dumps(request) + "\n"), output, session_factory=Broken) == 2
    assert closed == [True]
    assert json.loads(output.getvalue()) == {"id": 1, "ok": False,
                                            "error": "source_read_failed", "recoverable": False}
    assert "PRIVATE" not in output.getvalue()


@pytest.mark.parametrize("raw_command", ["{}\n", "[]\n", '{"id":true,"command":"close"}\n',
                                     '{"id":1,"command":"send"}\n', "x" * 262145 + "\n"],
                         ids=["empty", "array", "bool-id", "send-forbidden", "oversized"])
def test_invalid_worker_commands_do_not_create_native_session(raw_command):
    output = io.StringIO()
    assert db_worker.serve(io.StringIO(raw_command), output,
                           session_factory=lambda *_: pytest.fail("native preparation forbidden")) == 2
    assert json.loads(output.getvalue())["ok"] is False


def test_worker_main_refuses_user_visible_terminal(monkeypatch):
    monkeypatch.setattr(db_worker.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(db_worker, "serve", lambda *_: pytest.fail("private protocol must use pipes"))
    assert db_worker.main() == 2
