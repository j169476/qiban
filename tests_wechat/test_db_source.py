"""Production-source semantics using synthetic SQLite and fake preparation."""
from __future__ import annotations

import copy
import hashlib
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

    def session(self, enrollment=None, checkpoint=None, **options):
        return ds.SourceSession({}, enrollment, checkpoint, prepare_fn=self.prepare, clock=lambda: SINCE,
                                state_fn=lambda _: {"window_count": 1}, **options)

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


def test_frozen_window_replays_and_resumes_without_absorbing_new_arrivals(lab):
    with lab.session() as session:
        snapshot = session.snapshot()
        lab.insert(1, text="same body")
        lab.insert(2, text="same body")
        window = session.freeze_window(snapshot["checkpoint"])
        first = session.read_window(snapshot["checkpoint"], window, limit=2)
        assert [r["server_id"] for r in first["rows"]] == ["1001"]
        assert first["window_complete"] is False
        lab.insert(3, text="later window")
        assert session.read_window(snapshot["checkpoint"], window, limit=2) == first
    with lab.session(snapshot["enrollment"], first["next_checkpoint"]) as resumed:
        second = resumed.read_window(first["next_checkpoint"], window, limit=2)
        assert [r["server_id"] for r in second["rows"]] == ["1002"]
        assert second["window_complete"] is True
        exhausted = resumed.read_window(second["next_checkpoint"], window, limit=2)
        assert exhausted["rows"] == []
        assert exhausted["window_complete"] is True
        following = resumed.freeze_window(second["next_checkpoint"])
        third = resumed.read_window(second["next_checkpoint"], following, limit=2)
        assert [r["text"] for r in third["rows"]] == ["later window"]
        assert third["window_complete"] is True


def test_window_completion_checks_all_sources_even_when_selected_page_is_empty(lab):
    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        lab.insert(1, target=OWNER)
        window = session.freeze_window(checkpoint)
        # Sorted family/group source is empty, but owner_main still has work.
        first = session.read_window(checkpoint, window, limit=1)
        assert first["rows"] == []
        assert first["window_complete"] is False
        second = session.read_window(first["next_checkpoint"], window, limit=1)
        assert len(second["rows"]) == 1
        assert second["window_complete"] is True


@pytest.mark.parametrize("fault", ["epoch", "source", "salt", "upper_identity", "position"])
def test_invalid_or_changed_window_cannot_expand_read_scope(lab, fault):
    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        lab.insert(1)
        window = session.freeze_window(checkpoint)
        key = next(k for k in window["sources"] if k.endswith("|owner_main"))
        if fault == "epoch":
            window["epoch"] = "f" * 32
        elif fault == "source":
            del window["sources"][key]
        elif fault == "salt":
            window["sources"][key]["salt"] = "f" * 64
        elif fault == "upper_identity":
            window["sources"][key]["upper"]["server_id"] += 1
        else:
            checkpoint["sources"][key]["anchor"] = {"local_id": 2, "server_id": 1002, "create_time": SINCE + 1}
        with pytest.raises(ds.SourceError):
            session.read_window(checkpoint, window)


def test_window_worker_commands_return_only_their_requested_protocol_fields(lab):
    with lab.session() as session:
        checkpoint = session.snapshot()["checkpoint"]
        lab.insert(1)
        window = session.freeze_window(checkpoint)
        # Use a separate initialized session through the production dispatcher.
        snapshot = session.snapshot()
    requests = [
        {"id": 1, "command": "open", "config": {}, **snapshot},
        {"id": 2, "command": "freeze_window", "checkpoint": checkpoint},
        {"id": 3, "command": "read_window", "checkpoint": checkpoint, "window": window, "limit": 100},
        {"id": 4, "command": "close"},
    ]
    output = io.StringIO()
    assert db_worker.serve(io.StringIO("".join(json.dumps(r) + "\n" for r in requests)), output,
                           session_factory=lambda config, enrollment, checkpoint: lab.session(enrollment, checkpoint)) == 0
    results = [json.loads(line) for line in output.getvalue().splitlines()]
    assert all(r["ok"] for r in results)
    assert results[1]["result"] == window
    assert results[2]["result"]["window_complete"] is True
    assert len(results[2]["result"]["rows"]) == 1


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

def test_alias_lookup_reads_current_bound_contact_and_requires_online(lab):
    import sqlite3
    from contextlib import contextmanager, nullcontext
    contacts = sqlite3.connect(":memory:")
    contacts.execute("CREATE TABLE contact(username TEXT,alias TEXT)")
    contacts.execute("INSERT INTO contact VALUES (?,?)",(OWNER,"alias-test"))
    @contextmanager
    def prepared(config,enrollment):
        with lab.prepare(config,enrollment) as value:
            value.open_contacts=lambda: nullcontext(contacts)
            yield value
    windows = {"window_count":1}
    try:
        with ds.SourceSession({},prepare_fn=prepared,clock=lambda:SINCE,state_fn=lambda pid:windows) as session:
            assert session.resolve_alias("alias-test")=={"username":OWNER}
            contacts.execute("UPDATE contact SET alias='alias-new'")
            assert session.resolve_alias("alias-test")=={"username":None}
            contacts.execute("INSERT INTO contact VALUES ('outside','alias-new')")
            assert session.resolve_alias("alias-new")=={"username":None}
            windows["window_count"]=0
            with pytest.raises(ds.SourceError,match="client_not_running"):
                session.resolve_alias("alias-test")
    finally:
        contacts.close()

def test_first_seen_since_broad_consent_preserves_boundary_and_durable_cursor(lab):
    lab.insert(1,created=SINCE-2,text="before-consent")
    lab.insert(2,created=SINCE,text="first-offline-message")
    lab.insert(3,created=SINCE-1,text="older-timestamp-in-later-row")
    lab.insert(4,created=SINCE+1,text="second-offline-message")
    with lab.session(authorized_since=SINCE) as session:
        initial=session.snapshot()
        assert initial["enrollment"]["since"]==SINCE
        result=session.read(initial["checkpoint"])
        assert [r["text"] for r in result["rows"]]==["first-offline-message","second-offline-message"]
    with lab.session(initial["enrollment"],result["next_checkpoint"]) as resumed:
        assert resumed.read(result["next_checkpoint"])["rows"]==[]


def test_bulk_verification_uses_exact_current_relationships_across_sql_pages():
    from contextlib import closing
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE contact(username,local_type,flag,delete_flag,verify_flag,is_in_chat_room)")
        targets = [{"kind": "private", "username": f"user{i}"} for i in range(502)]
        db.executemany("INSERT INTO contact VALUES (?,1,1,0,0,0)", [(t["username"],) for t in targets])
        targets += [{"kind": "group", "username": "group@chatroom"}, {"kind": "private", "username": "me"}]
        db.execute("INSERT INTO contact VALUES ('group@chatroom',2,0,0,0,1),('me',1,1,0,0,0)")
        db.commit()
        assert ds.verify_contacts(db, "me", targets) == {"verified": 503, "unavailable": ["me"]}
        db.execute("UPDATE contact SET delete_flag=1 WHERE username='user500'")
        db.execute("UPDATE contact SET is_in_chat_room=0 WHERE username='group@chatroom'")
        db.execute("INSERT INTO contact VALUES ('user2',1,1,0,0,0)")
        db.commit()
        assert ds.verify_contacts(db, "me", targets) == {
            "verified": 500, "unavailable": ["user2", "user500", "group@chatroom", "me"]}
        with pytest.raises(ds.SourceError):
            ds.verify_contacts(db, "me", targets * 2)


def test_saved_roster_can_wait_for_first_message_and_keep_unrelated_cursor(lab):
    with lab.session() as first:
        initial = first.snapshot()
    enrollment = {**initial["enrollment"], "schema_version": 2,
                  "target_since": {name:SINCE+2 for name in initial["enrollment"]["targets"]}}
    empty = {**initial["checkpoint"], "sources": {}}
    conn = lab.shards["message_0.db"]
    conn.execute(f'DROP TABLE "{message_table(OWNER)}"')
    with lab.session(enrollment, empty, recover_missing_history=True) as waiting:
        pointer = waiting.snapshot()
        assert all(i["target_ref"] != "owner_main" for i in pointer["checkpoint"]["sources"].values())
    conn.execute(f'CREATE TABLE "{message_table(OWNER)}"(local_id INTEGER PRIMARY KEY, server_id, create_time, '
                 'local_type, real_sender_id, message_content, sort_seq)')
    lab.insert(1,created=SINCE+1,text="before-selection")
    lab.insert(2,created=SINCE+2,text="eligible")
    with lab.session(enrollment,pointer["checkpoint"],recover_missing_history=True) as live:
        cp=live.snapshot()["checkpoint"]
        data=live.read_window(cp,live.freeze_window(cp))
        assert [r["text"] for r in data["rows"]] == ["eligible"]
    conn.execute(f'DROP TABLE "{message_table(OWNER)}"')
    with lab.session(enrollment,data["next_checkpoint"],recover_missing_history=True) as lost:
        cp=lost.snapshot()["checkpoint"]
        data=lost.read_window(cp,lost.freeze_window(cp))
        assert data["window_complete"] and not data["rows"]
        assert data["history_status"]["owner_main"]["status"]=="incomplete"


def test_returned_member_reports_missing_anchor_without_resetting_to_first_message(lab,monkeypatch):
    kind = ["private"]
    with lab.session() as session:
        monkeypatch.setattr(session,"member_kind",lambda target:kind[0])
        session._prepared.shards={lab.account/"db_storage/message"/name:db for name,db in lab.shards.items()}
        lab.insert(1,created=SINCE,text="old")
        lab.insert(2,created=SINCE+1,text="remaining")
        first=session.member_window(OWNER,"private",SINCE,None,None,1)
        assert not first["window_complete"]
        assert [r["text"] for r in first["rows"]]==["old"]
        kind[0]=None
        assert session.member_window(OWNER,"private",SINCE,first["next_checkpoint"],None,1)=={"present":False}
        lab.shards["message_0.db"].execute(f'DELETE FROM "{message_table(OWNER)}" WHERE local_id=1')
        kind[0]="private"
        resumed=session.member_window(OWNER,"private",SINCE,first["next_checkpoint"],None,1)
        assert [r["text"] for r in resumed["rows"]]==["remaining"] and resumed["window_complete"]
        assert list(resumed["history_status"].values())==[{"status":"incomplete","reasons":["checkpoint_unavailable"]}]


def test_directory_pages_use_same_membership_as_exact_check_and_skip_ambiguous_ids(lab):
    from contextlib import closing, nullcontext

    with closing(sqlite3.connect(":memory:")) as contacts, lab.session() as session:
        contacts.execute("CREATE TABLE contact(username, local_type, flag, delete_flag, verify_flag, "
                         "is_in_chat_room, remark, nick_name)")
        rows = [
            ("a@chatroom", 2, 0, 0, 0, 0, "", "已退出"),
            ("b@chatroom", 2, 0, 0, 0, 1, "", "在群"),
            ("c@chatroom", 2, 0, 0, 0, 1, "", "重复"),
            ("c@chatroom", 1, 1, 0, 0, 0, "", "冲突"),
            ("d", 1, 3, 0, 0, 0, "备注", "昵称"),
            ("e", 1, 1, 1, 0, 0, "", "已删除"),
            (SELF, 1, 1, 0, 0, 0, "", "本人"),
        ]
        contacts.executemany("INSERT INTO contact VALUES(?,?,?,?,?,?,?,?)", rows)
        session._prepared.open_contacts = lambda: nullcontext(contacts)
        for kind, expected in [("group", [{"username": "b@chatroom", "name": "在群"}]),
                               ("private", [{"username": "d", "name": "备注"}])]:
            items, cursor = [], ""
            while cursor is not None:
                page = session.directory_page(kind, cursor, 1)
                items.extend(page["items"])
                assert page["next"] is None or page["next"] > cursor
                cursor = page["next"]
            assert items == expected
            assert session.member_page(kind, None) == {"ids":[i["username"] for i in expected], "next":None}
            assert {i["username"] for i in items} == {r[0] for r in rows if session.member_kind(r[0]) == kind}
        contacts.execute("UPDATE contact SET is_in_chat_room=0 WHERE username='b@chatroom'")
        assert session.directory_page("group", "", 100) == {"items": [], "next": None}


def test_member_pages_use_one_snapshot_without_display_columns_and_expire_cursor(lab):
    from contextlib import closing, nullcontext
    with closing(sqlite3.connect(":memory:")) as contacts, lab.session() as session:
        contacts.execute("CREATE TABLE contact(username,local_type,flag,delete_flag,verify_flag,is_in_chat_room)")
        contacts.executemany("INSERT INTO contact VALUES (?,1,1,0,0,0)",[(f"id{i:04}",) for i in range(258)])
        session._prepared.open_contacts = lambda: nullcontext(contacts)
        first = session.member_page("private",None)
        assert len(first["ids"]) == 256
        contacts.execute("DELETE FROM contact WHERE username='id0257'")
        assert session.member_page("private",first["next"]) == {"ids":["id0256","id0257"],"next":None}
        with pytest.raises(ds.SourceError):
            session.member_page("private",first["next"])
        fresh = session.member_page("private",None)
        assert session.member_page("private",fresh["next"]) == {"ids":["id0256"],"next":None}

def test_worker_does_not_release_backfill_if_membership_changes_during_read(lab):
    from contextlib import contextmanager, nullcontext
    contacts=sqlite3.connect(":memory:")
    contacts.execute("CREATE TABLE contact(username,local_type,flag,delete_flag,verify_flag,is_in_chat_room)")
    contacts.execute("INSERT INTO contact VALUES (?,1,3,0,0,0)",(OWNER,))
    lab.insert(1,created=SINCE,text="must-never-be-returned")
    @contextmanager
    def prepare(config,enrollment):
        with lab.prepare(config,enrollment) as p:
            p.targets=[t for t in p.targets if t.username==OWNER]
            p.sources=[s for s in p.sources if s.target.username==OWNER]
            p.open_contacts=lambda:nullcontext(contacts)
            yield p
    class Changing(ds.SourceSession):
        def read(self,checkpoint,**kwargs):
            result=super().read(self.snapshot()["checkpoint"],**kwargs)
            contacts.execute("UPDATE contact SET delete_flag=1")
            return result
    def factory(cfg,enrol,cp,**kwargs):
        return Changing(cfg,enrol,cp,prepare_fn=prepare,state_fn=lambda p:{"window_count":1},**kwargs)
    account="wechat-db:"+hashlib.sha256(SELF.encode()).hexdigest()
    commands=[{"id":1,"command":"open","config":{},"enrollment":None,"checkpoint":None,
               "audience":{"account_id":account,"kind":"private","username":OWNER,"since":SINCE}},
              {"id":2,"command":"read","checkpoint":{},"limit":100}]
    output=io.StringIO()
    try:
        assert db_worker.serve(io.StringIO("".join(json.dumps(c)+"\n" for c in commands)),output,session_factory=factory)==2
        rows=[json.loads(row) for row in output.getvalue().splitlines()]
        assert rows[0]["ok"] and rows[1]["error"]=="source_identity_changed"
        assert "must-never-be-returned" not in output.getvalue()
    finally:
        contacts.close()
