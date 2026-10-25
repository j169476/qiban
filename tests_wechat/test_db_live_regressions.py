"""Generated encrypted fixtures only: continuous commits and multiline rows."""

import json
import os
import queue
import subprocess
import sys
import threading

import pytest

from companion_wechat.db_probe import (
    MarkerTracker,
    Target,
    make_sources,
    message_table,
    read_since_baseline,
)
from companion_wechat.db_sqlite import open_readonly

_KEY = bytes(range(32))
_PREFIX = "XLD-0123ABCD"
_OWNER = "synthetic_owner"
_TARGETS = [Target("owner_main", _OWNER), Target("family", "synthetic_family@chatroom")]
_WRITER = r'''
import json,sys,sqlcipher3
c=sqlcipher3.connect(sys.argv[1],isolation_level=None,timeout=0.2)
c.set_key("x'"+bytes(range(32)).hex()+"'")
for sql in ("PRAGMA cipher_compatibility=4", "PRAGMA temp_store=MEMORY",
            "PRAGMA journal_mode=WAL", "PRAGMA wal_autocheckpoint=0",
            "CREATE TABLE Name2Id(user_name TEXT)",
            "INSERT INTO Name2Id VALUES('synthetic_owner')"):
    c.execute(sql)
tables=json.loads(sys.argv[2])
for table in tables:
    c.execute(f'CREATE TABLE "{table}"(local_id INTEGER PRIMARY KEY,server_id INTEGER,'
              'local_type INTEGER,real_sender_id INTEGER,message_content TEXT)')
    c.execute(f'INSERT INTO "{table}" VALUES(1,1001,1,1,?)',('synthetic historical placeholder',))
print(json.dumps("ready"),flush=True)
for raw in sys.stdin:
    cmd=json.loads(raw)
    if cmd is None:
        break
    if cmd[0]=="checkpoint":
        result=c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    else:
        index,number=cmd[1:]
        for table,letter in zip(tables,('P','G')):
            numbers=(1,2,2,3) if cmd[0]=="multiline" else (number,)
            text='\n'.join(f'XLD-0123ABCD-{letter}-0{n}' for n in numbers)
            if letter=='G':
                text='synthetic_owner:\n'+text
            c.execute(f'INSERT INTO "{table}" VALUES(?,?,1,1,?)',(index,1000+index,text))
        result="done"
    print(json.dumps(result),flush=True)
c.close()
'''


@pytest.fixture
def encrypted_poll_writer(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows-only SQLCipher readonly_shm integration")
    pytest.importorskip("sqlcipher3")
    path = tmp_path / "synthetic.db"
    # Keep writers outside the reader process: SQLite's Windows VFS otherwise
    # reuses an existing writable SHM mapping even for readonly_shm connections.
    process = subprocess.Popen(
        [sys.executable, "-c", _WRITER, str(path), json.dumps([message_table(t.username) for t in _TARGETS])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    replies = queue.Queue()

    def read_replies():
        for line in process.stdout:
            replies.put(line)
        replies.put(None)

    reader = threading.Thread(target=read_replies, daemon=True)
    reader.start()

    def receive():
        reply = replies.get(timeout=10)
        assert reply is not None, "synthetic writer exited before replying"
        return json.loads(reply)

    def command(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()
        return receive()

    try:
        assert receive() == "ready"
        assert path.read_bytes()[:16] != b"SQLite format 3\0"
        yield path, command
    finally:
        if process.poll() is None:
            try:
                process.stdin.write("null\n")
                process.stdin.flush()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=10)
        reader.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def _poll(sources, tracker):
    batches = [read_since_baseline(source) for source in sources]
    for source, rows in zip(sources, batches):
        for row in rows:
            tracker.observe("synthetic", source.target.alias, row, minimized=True)
    return batches


def test_each_commit_visible_across_polls_and_wal_truncation(encrypted_poll_writer):
    path, command = encrypted_poll_writer
    tracker = MarkerTracker(_OWNER, _PREFIX)
    with open_readonly(path, _KEY) as connection:
        sources = make_sources(connection, path, _TARGETS)
        assert len(sources) == 2
        assert [len(rows) for rows in _poll(sources, tracker)] == [0, 0]
        for local_id, number in enumerate((1, 2, 2, 3), start=2):
            assert command(["insert", local_id, number]) == "done"
            batches = _poll(sources, tracker)
            assert [len(rows) for rows in batches] == [local_id - 1, local_id - 1]
            assert all(state["new_rows"] == local_id - 1 for state in tracker.targets.values())
            # A cursor/transaction leaked across polls would pin the old WAL.
            assert command(["checkpoint"])[0] == 0
        _poll(sources, tracker)  # Re-observing the same rows cannot count them twice.
        for state in tracker.targets.values():
            assert state["new_rows"] == 4
            assert state["marker_counts"] == [1, 2, 1]
            assert state["minimized_marker_counts"] == [1, 2, 1]
            assert state["unverified_sender_markers"] == state["unstable_id_markers"] == 0


def test_multiline_marker_body_is_one_row_not_four_messages(encrypted_poll_writer):
    path, command = encrypted_poll_writer
    tracker = MarkerTracker(_OWNER, _PREFIX)
    with open_readonly(path, _KEY) as connection:
        sources = make_sources(connection, path, _TARGETS)
        assert command(["multiline", 2, None]) == "done"
        batches = _poll(sources, tracker)
        assert [len(rows) for rows in batches] == [1, 1]
        # Private body has four lines; the group adds its stable sender prefix.
        assert [len(rows[0][4].splitlines()) for rows in batches] == [4, 5]
        assert all(rows[0][1] > 0 and rows[0][3] == _OWNER for rows in batches)
        assert command(["checkpoint"])[0] == 0
        _poll(sources, tracker)
        for state in tracker.targets.values():
            assert state["new_rows"] == 1
            assert state["marker_counts"] == [0, 0, 0]
            assert state["minimized_marker_counts"] == [0, 0, 0]
