"""Read only a verified self row and refuse ambiguous or stale identities."""
import io
import json
import sqlite3
from contextlib import contextmanager

import pytest

from companion_wechat import db_setup
from companion_wechat.db_probe import ProbeError
from companion_wechat.db_worker import serve


@pytest.mark.parametrize('rows,valid', [
    ([("one", "同名🙂", "my_wechat"), ("other", "同名🙂", "not_mine")], True),
    ([("one", "昵称", None)], True),
    ([("other", "其他人", "not_mine")], False),
    ([("one", "名字", "id1"), ("one", "名字", "id2")], False),
])
def test_exact_self_only(monkeypatch, rows, valid):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE contact(username TEXT,nick_name TEXT,alias TEXT)')
    db.executemany('INSERT INTO contact VALUES(?,?,?)', rows)
    queries = []
    db.set_trace_callback(queries.append)

    @contextmanager
    def contacts(login):
        assert login == 'one'
        yield db, None
    monkeypatch.setattr(db_setup, '_contacts', contacts)
    try:
        if valid:
            result = db_setup.account_details('one')
            assert result == {'login': 'one', 'nickname': rows[0][1], 'wechat_id': rows[0][2] or ''}
            assert 'not_mine' not in str(result)
        else:
            with pytest.raises(ProbeError, match='source_identity_changed'):
                db_setup.account_details('one')
        assert all('message' not in q and ('PRAGMA' in q or "WHERE username='one' COLLATE BINARY LIMIT 2" in q) for q in queries)
    finally:
        db.close()


def test_process_switch_at_close_discards_metadata(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE contact(username TEXT,nick_name TEXT,alias TEXT)')
    db.execute("INSERT INTO contact VALUES('one','nickname','id')")

    @contextmanager
    def contacts(login):
        yield db, None
        raise ProbeError('source_identity_changed')
    monkeypatch.setattr(db_setup, '_contacts', contacts)
    try:
        with pytest.raises(ProbeError, match='source_identity_changed'):
            db_setup.account_details('one')
    finally:
        db.close()


def test_worker_returns_only_self_metadata_without_opening_message_session(monkeypatch):
    monkeypatch.setattr(db_setup, 'account_details', lambda login: {'login': login, 'nickname': '名字', 'wechat_id': ''})
    output = io.StringIO()
    assert serve(io.StringIO('{"id":1,"command":"account_details","login":"one"}\n'), output,
                 session_factory=lambda *args: pytest.fail('must not open messages')) == 0
    assert json.loads(output.getvalue())['result'] == {'login': 'one', 'nickname': '名字', 'wechat_id': ''}
