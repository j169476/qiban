"""Synthetic SQLite rows and synthetic page headers; never access WeChat data."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from companion_wechat import db_probe
from companion_wechat.db_probe import (
    MarkerTracker,
    ProbeError,
    Target,
    make_sources,
    marker_number,
    message_table,
    read_since_baseline,
    resolve_targets,
)

OWNER = "synthetic_owner_identifier"
GROUP = "synthetic_family_identifier@chatroom"
OTHER = "synthetic_other_identifier"
PREFIX = "XLD-A1B2C3D4"
SALT = bytes(range(16))


@pytest.fixture
def connection():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    yield conn
    conn.close()


def test_probe_passes_explicit_pid_to_discovery_before_any_key_access(tmp_path, monkeypatch):
    from companion_wechat import db_windows

    selected = []

    def discover(*, pid=None):
        selected.append(pid)
        raise db_windows.WindowsProbeError("client_not_running")

    monkeypatch.setattr(db_windows, "discover_wechat", discover)
    monkeypatch.setattr(db_windows, "NativeProcess", lambda *_: pytest.fail("no process scan after discovery fails"))
    config = {"account_dirs": [str(tmp_path)], "private_hint": OWNER, "group_hint": "synthetic group",
              "client_pid": 77}
    with pytest.raises(db_windows.WindowsProbeError, match="client_not_running"):
        with db_probe.prepare(config):
            pytest.fail("unreachable")
    assert selected == [77]


def contacts(conn, *, nocase=False):
    collation = " COLLATE NOCASE" if nocase else ""
    conn.execute("CREATE TABLE contact (" + ",".join(
        f"{name} TEXT{collation}" for name in ("username", "alias", "nick_name", "remark")
    ) + ")")
    conn.executemany("INSERT INTO contact VALUES (?,?,?,?)", [
        (OWNER, "synthetic-private-alias", "private display", "private remark"),
        (GROUP, "", "synthetic-family-name", "synthetic-family-remark"),
    ])


def schema(conn, username=OWNER):
    table = message_table(username)
    conn.execute("CREATE TABLE IF NOT EXISTS Name2Id (user_name TEXT)")
    conn.execute("INSERT OR IGNORE INTO Name2Id (rowid,user_name) VALUES (1,?)", (OWNER,))
    conn.execute("INSERT OR IGNORE INTO Name2Id (rowid,user_name) VALUES (2,?)", (OTHER,))
    conn.execute(f'CREATE TABLE "{table}" (local_id INTEGER PRIMARY KEY,server_id,'
                 'local_type,real_sender_id,message_content,sort_seq INTEGER)')
    return table


def insert(conn, table, local_id, *, server_id=None, sender_id=1, content="synthetic private body",
           local_type=1, sort_seq=99):
    conn.execute(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?)',
                 (local_id, server_id, local_type, sender_id, content, sort_seq))


def source(conn, tmp_path, *, username=OWNER, alias="owner_main", table=None):
    if table is None:
        table = schema(conn, username)
    path = tmp_path / f"{alias}-synthetic.db"
    path.write_bytes(SALT + bytes(4096 - 16))
    return make_sources(conn, path, [Target(alias, username)])[0]


def marker(number=1, alias="owner_main"):
    return f"{PREFIX}-{'P' if alias == 'owner_main' else 'G'}-0{number}"


def observation(local_id=1, server_id=101, number=1, *, sender=OWNER, alias="owner_main"):
    return local_id, server_id, 1, sender, marker(number, alias)


@pytest.mark.parametrize("private_hint", [OWNER, "synthetic-private-alias"])
@pytest.mark.parametrize("group_hint", ["synthetic-family-name", "synthetic-family-remark"])
def test_resolve_targets_matches_exact_private_identity_and_group_labels(connection, private_hint, group_hint):
    contacts(connection)
    result = resolve_targets(connection, private_hint, group_hint)
    assert [(item.alias, item.username) for item in result] == [("owner_main", OWNER), ("family", GROUP)]


@pytest.mark.parametrize("private_hint,group_hint", [
    ("synthetic-private", "synthetic-family-name"),
    ("private display", "synthetic-family-name"),
    (OWNER, "synthetic-family"),
    (" synthetic-private-alias", "synthetic-family-name"),
    ("' OR 1=1 --", "synthetic-family-name"),
])
def test_partial_names_display_names_and_sql_fragments_do_not_select_targets(connection, private_hint, group_hint):
    contacts(connection)
    with pytest.raises(ProbeError, match="^target_missing_or_ambiguous$"):
        resolve_targets(connection, private_hint, group_hint)


@pytest.mark.parametrize("row", [
    (OTHER, "synthetic-private-alias", "", ""),
    ("second@chatroom", "", "synthetic-family-name", ""),
])
def test_two_distinct_matching_identities_are_ambiguous(connection, row):
    contacts(connection)
    connection.execute("INSERT INTO contact VALUES (?,?,?,?)", row)
    with pytest.raises(ProbeError, match="^target_missing_or_ambiguous$"):
        resolve_targets(connection, "synthetic-private-alias", "synthetic-family-name")


def test_duplicate_rows_cannot_hide_a_later_ambiguous_identity(connection):
    contacts(connection)
    connection.executemany("INSERT INTO contact VALUES (?,?,?,?)", [
        (OWNER, "synthetic-private-alias", "", ""),
        (OWNER, "synthetic-private-alias", "", ""),
        (OTHER, "synthetic-private-alias", "", ""),
    ])
    with pytest.raises(ProbeError, match="^target_missing_or_ambiguous$"):
        resolve_targets(connection, "synthetic-private-alias", "synthetic-family-name")


def test_exact_matching_does_not_inherit_nocase_column_collation(connection):
    contacts(connection, nocase=True)
    with pytest.raises(ProbeError, match="^target_missing_or_ambiguous$"):
        resolve_targets(connection, "SYNTHETIC-PRIVATE-ALIAS", "synthetic-family-name")


def test_distinct_matching_accounts_are_not_merged_by_nocase_collation(connection):
    contacts(connection, nocase=True)
    connection.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       (OWNER.upper(), "synthetic-private-alias", "", ""))
    with pytest.raises(ProbeError, match="^target_missing_or_ambiguous$"):
        resolve_targets(connection, "synthetic-private-alias", "synthetic-family-name")


def test_private_target_cannot_resolve_to_group(connection):
    contacts(connection)
    with pytest.raises(ProbeError, match="^target_kind_mismatch$"):
        resolve_targets(connection, GROUP, "synthetic-family-name")


def test_contact_schema_is_required(connection):
    connection.execute("CREATE TABLE contact (username TEXT)")
    with pytest.raises(ProbeError, match="^contact_schema_unsupported$"):
        resolve_targets(connection, OWNER, "synthetic-family-name")


@pytest.mark.parametrize("username", [OWNER, GROUP, '"; DROP TABLE contact; --', "合成账号"])
def test_message_table_uses_only_full_username_hash(username):
    result = message_table(username)
    assert result == "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()
    assert len(result) == 36 and all(char in "0123456789abcdef" for char in result[4:])


def test_baseline_skips_history_and_equal_sort_sequences_are_all_read(connection, tmp_path):
    table = schema(connection)
    insert(connection, table, 3, content="synthetic historical body")
    item = source(connection, tmp_path, table=table)
    assert item.baseline == 3
    insert(connection, table, 4, server_id=401)
    insert(connection, table, 5, server_id=402)
    assert [row[:2] for row in read_since_baseline(item)] == [(4, 401), (5, 402)]
    assert item.baseline == 3
    assert not connection.in_transaction


def test_read_joins_sender_by_rowid_without_guessing_missing_mapping(connection, tmp_path):
    item = source(connection, tmp_path)
    insert(connection, item.table, 1, sender_id=2)
    insert(connection, item.table, 2, sender_id=999)
    assert [row[3] for row in read_since_baseline(item)] == [OTHER, None]


def test_read_revisits_pending_row_after_server_id_is_filled(connection, tmp_path):
    item = source(connection, tmp_path)
    insert(connection, item.table, 1, server_id=0, content=marker())
    tracker = MarkerTracker(OWNER, PREFIX)
    tracker.observe("synthetic_shard", "owner_main", read_since_baseline(item)[0], False)
    connection.execute(f'UPDATE "{item.table}" SET server_id=101 WHERE local_id=1')
    tracker.observe("synthetic_shard", "owner_main", read_since_baseline(item)[0], True)
    assert tracker.targets["owner_main"]["new_rows"] == 1
    assert tracker.targets["owner_main"]["marker_counts"] == [1, 0, 0]


def test_read_revisits_pending_sender_mapping_when_name_table_is_filled(connection, tmp_path):
    item = source(connection, tmp_path)
    insert(connection, item.table, 1, server_id=101, sender_id=999, content=marker())
    tracker = MarkerTracker(OWNER, PREFIX)
    tracker.observe("synthetic_shard", "owner_main", read_since_baseline(item)[0], False)
    connection.execute("INSERT INTO Name2Id (rowid,user_name) VALUES (999,?)", (OWNER,))
    tracker.observe("synthetic_shard", "owner_main", read_since_baseline(item)[0], True)
    assert tracker.targets["owner_main"]["marker_counts"] == [1, 0, 0]


def test_only_bounded_text_messages_are_returned_for_marker_inspection(connection, tmp_path):
    item = source(connection, tmp_path)
    insert(connection, item.table, 1, local_type=3, content="synthetic image body")
    insert(connection, item.table, 2, content="x" * 4097)
    insert(connection, item.table, 3, content="x" * 4096)
    rows = read_since_baseline(item)
    assert rows[0][4] is None and rows[1][4] is None and len(rows[2][4]) == 4096


@pytest.mark.parametrize("content", ["x\x00" + "x" * 10000, "文" * 2000], ids=["nul", "utf8_bytes"])
def test_text_limit_is_a_byte_bound_and_cannot_be_bypassed_by_nul(connection, tmp_path, content):
    item = source(connection, tmp_path)
    insert(connection, item.table, 1, content=content)
    assert read_since_baseline(item)[0][4] is None


def test_row_limit_fails_closed_and_rolls_back_transaction(connection, tmp_path):
    item = source(connection, tmp_path)
    for number in range(1, 4):
        insert(connection, item.table, number)
    assert len(read_since_baseline(item, limit=3)) == 3
    with pytest.raises(ProbeError, match="^observation_limit_exceeded$"):
        read_since_baseline(item, limit=2)
    assert not connection.in_transaction


@pytest.mark.parametrize("limit", [-2, -1, 0, 2001, True, 1.5, "2"])
def test_invalid_read_limits_are_rejected_before_executing_queries(connection, tmp_path, limit):
    item = source(connection, tmp_path)
    queries = []
    connection.set_trace_callback(queries.append)
    with pytest.raises(ProbeError):
        read_since_baseline(item, limit=limit)
    assert not queries


@pytest.mark.parametrize("change", ["salt", "inode", "maximum"])
def test_database_generation_changes_are_not_silently_rebased(connection, tmp_path, change):
    table = schema(connection)
    insert(connection, table, 10)
    item = source(connection, tmp_path, table=table)
    if change == "salt":
        item.path.write_bytes(bytes(reversed(SALT)) + bytes(4096 - 16))
    elif change == "inode":
        item.inode += 1
    else:
        connection.execute(f'DELETE FROM "{table}"')
    with pytest.raises(ProbeError, match="^database_generation_changed$"):
        read_since_baseline(item)
    assert not connection.in_transaction


def test_restarting_experiment_gets_fresh_baseline_and_never_replays_old_bodies(connection, tmp_path):
    item = source(connection, tmp_path)
    insert(connection, item.table, 1, content=marker())
    assert len(read_since_baseline(item)) == 1
    restarted = make_sources(connection, item.path, [item.target])[0]
    assert restarted.baseline == 1 and read_since_baseline(restarted) == []


@pytest.mark.parametrize("contents", [b"x" * 4095, b"SQLite format 3\x00" + bytes(4080)],
                         ids=["truncated", "plaintext"])
def test_page_one_rejects_truncated_or_plaintext_databases(tmp_path, contents):
    path = tmp_path / "synthetic.db"
    path.write_bytes(contents)
    with pytest.raises(ProbeError, match="^database_format_unsupported$"):
        db_probe.page_one(path)


def test_source_discovery_does_not_substitute_an_unrelated_message_table(connection, tmp_path):
    schema(connection, OTHER)
    path = tmp_path / "synthetic.db"
    path.write_bytes(SALT + bytes(4080))
    assert make_sources(connection, path, [Target("owner_main", OWNER)]) == []


@pytest.mark.parametrize("payload", [
    "XLD-00000000-P-01", f"{PREFIX}-G-01", f"{PREFIX}-P-00", f"{PREFIX}-P-04",
    f"{PREFIX}-P-010", f"prefix {PREFIX}-P-01", f"{PREFIX}-P-01\nextra",
    b"\xff", "x" * 4097, None, 1,
], ids=["wrong_run", "wrong_target", "zero", "four", "extra_digit", "embedded",
        "extra_line", "bad_utf8", "long", "none", "integer"])
def test_bad_markers_are_not_recognized(payload):
    assert marker_number(payload, PREFIX, "owner_main", OWNER) is None


def test_group_sender_prefix_must_match_the_mapped_sender():
    payload = OWNER + ":\n" + marker(2, "family")
    assert marker_number(payload, PREFIX, "family", OWNER) == 2
    assert marker_number(payload.encode(), PREFIX, "family", OWNER) == 2
    assert marker_number(payload, PREFIX, "family", OTHER) is None
    assert marker_number(payload, PREFIX, "family", None) is None
    assert marker_number(OWNER + ":\n" + marker(), PREFIX, "owner_main", OWNER) is None


def test_same_local_id_on_different_shards_does_not_collide():
    tracker = MarkerTracker(OWNER, PREFIX)
    tracker.observe("shard_a", "owner_main", observation(server_id=101), True)
    tracker.observe("shard_b", "owner_main", observation(server_id=102), False)
    assert tracker.targets["owner_main"]["new_rows"] == 2
    assert tracker.targets["owner_main"]["marker_counts"] == [2, 0, 0]
    assert tracker.targets["owner_main"]["minimized_marker_counts"] == [1, 0, 0]


def test_same_server_id_across_shards_is_counted_once_and_repeat_polls_are_idempotent():
    tracker = MarkerTracker(OWNER, PREFIX)
    for shard in ("shard_a", "shard_b", "shard_a", "shard_b"):
        tracker.observe(shard, "owner_main", observation(), True)
    assert tracker.targets["owner_main"]["new_rows"] == 2
    assert tracker.targets["owner_main"]["marker_counts"] == [1, 0, 0]
    assert tracker.targets["owner_main"]["minimized_marker_counts"] == [1, 0, 0]


def test_repolling_after_minimize_does_not_reclassify_an_already_seen_marker():
    tracker = MarkerTracker(OWNER, PREFIX)
    tracker.observe("shard_a", "owner_main", observation(), False)
    tracker.observe("shard_a", "owner_main", observation(), True)
    assert tracker.targets["owner_main"]["marker_counts"] == [1, 0, 0]
    assert tracker.targets["owner_main"]["minimized_marker_counts"] == [0, 0, 0]


def test_identical_text_with_distinct_server_ids_is_preserved():
    tracker = MarkerTracker(OWNER, PREFIX)
    tracker.observe("shard_a", "owner_main", observation(1, 101), False)
    tracker.observe("shard_a", "owner_main", observation(2, 102), False)
    assert tracker.targets["owner_main"]["marker_counts"] == [2, 0, 0]


@pytest.mark.parametrize("second", [observation(1, 102), observation(1, 101, 2)])
def test_stable_local_marker_identity_changes_fail_closed(second):
    tracker = MarkerTracker(OWNER, PREFIX)
    tracker.observe("shard_a", "owner_main", observation(), False)
    with pytest.raises(ProbeError, match="^message_identity_conflict$"):
        tracker.observe("shard_a", "owner_main", second, False)


def test_one_server_id_cannot_describe_two_distinct_marker_numbers():
    tracker = MarkerTracker(OWNER, PREFIX)
    tracker.observe("shard_a", "owner_main", observation(), False)
    with pytest.raises(ProbeError, match="^message_identity_conflict$"):
        tracker.observe("shard_b", "owner_main", observation(2, 101, 2), False)


def test_other_sender_cannot_count_as_owner_marker():
    tracker = MarkerTracker(OWNER, PREFIX)
    tracker.observe("shard_a", "family", observation(sender=OTHER, alias="family"), True)
    assert tracker.targets["family"]["marker_counts"] == [0, 0, 0]
    assert tracker.targets["family"]["unverified_sender_markers"] == 1


def test_pending_observations_do_not_inflate_diagnostic_counts_on_each_poll():
    tracker = MarkerTracker(OWNER, PREFIX)
    for _ in range(3):
        tracker.observe("shard_a", "owner_main", observation(server_id=0), False)
        tracker.observe("shard_a", "owner_main", observation(2, 102, sender=None), False)
    assert tracker.targets["owner_main"]["unstable_id_markers"] == 1
    assert tracker.targets["owner_main"]["unverified_sender_markers"] == 1
    assert tracker.targets["owner_main"]["new_rows"] == 2


def test_tracker_does_not_retain_message_body_after_observation():
    tracker = MarkerTracker(OWNER, PREFIX)
    body = "SYNTHETIC CONFIDENTIAL CONTENT THAT MUST NOT BE RETAINED"
    tracker.observe("synthetic_shard", "owner_main", (1, 101, 1, OWNER, body), False)
    tracker.observe("synthetic_shard", "owner_main", observation(2, 102), False)
    assert body not in repr(vars(tracker)) and marker() not in repr(vars(tracker))


def test_marker_local_row_memory_is_bounded():
    tracker = MarkerTracker(OWNER, PREFIX)
    for number in range(1, 10001):
        tracker.observe("shard_a", "owner_main", (number, 0, 3, None, None), False)
    with pytest.raises(ProbeError, match="^observation_limit_exceeded$"):
        tracker.observe("shard_a", "owner_main", (10001, 0, 3, None, None), False)


def test_run_reports_never_include_message_content_paths_or_account_identifiers(
    connection, tmp_path, monkeypatch,
):
    item = source(connection, tmp_path)
    private_body = "SYNTHETIC CONFIDENTIAL BODY THAT MUST NOT APPEAR IN REPORTS"
    insert(connection, item.table, 1, server_id=101, content=private_body)
    insert(connection, item.table, 2, server_id=102, content=marker())
    targets = [Target("owner_main", OWNER), Target("family", GROUP)]

    @contextmanager
    def fake_prepare(_config):
        yield SimpleNamespace(pid=99, version="synthetic-version"), targets, [item]

    monkeypatch.setattr(db_probe, "prepare", fake_prepare)
    monkeypatch.setitem(sys.modules, "companion_wechat.db_windows", SimpleNamespace(
        window_state=lambda _pid: {"all_minimized": True, "foreground_is_target": False, "window_count": 1}
    ))
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(db_probe, "time", SimpleNamespace(
        monotonic=lambda: clock[0], process_time=lambda: 0.0, sleep=sleep,
    ))
    emitted = []
    report = db_probe.run({"account_dirs": [str(tmp_path)]}, duration=1, interval=1,
                          prefix=PREFIX, emit=lambda value: emitted.append(json.dumps(value)))
    serialized = json.dumps(report) + "".join(emitted)
    for forbidden in (private_body, OWNER, GROUP, str(tmp_path), item.table, marker()):
        assert forbidden not in serialized
    assert report["targets"]["owner_main"]["marker_counts"] == [1, 0, 0]
    assert report["targets"]["owner_main"]["minimized_marker_counts"] == [1, 0, 0]
    assert report["status"] == "watch_complete"
    assert report["message_content_exported"] is False
    assert report["messages_sent"] == 0 and report["core_events_created"] == 0
def test_confirmed_marker_cannot_change_to_nonmarker():
    from companion_wechat.db_probe import MarkerTracker, ProbeError

    tracker = MarkerTracker("synthetic_owner", "XLD-1234ABCD")
    tracker.observe("shard", "owner_main", (1, 99, 1, "synthetic_owner", "XLD-1234ABCD-P-01"), True)
    with pytest.raises(ProbeError, match="message_identity_conflict"):
        tracker.observe("shard", "owner_main", (1, 99, 1, "synthetic_owner", "changed"), True)

