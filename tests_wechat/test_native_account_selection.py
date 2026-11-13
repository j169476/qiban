import io
import json
from types import SimpleNamespace

import pytest

from companion_wechat.db_worker import serve
from companion_wechat.native.protocol import ProbeFailure
from companion_wechat.native.windows import Windows


@pytest.mark.parametrize("accounts,ok", [([], True), (["one", "two"], True), (["one", "one"], False)])
def test_discovery_never_opens_databases_or_exposes_process_handles(monkeypatch, accounts, ok):
    monkeypatch.setattr(Windows, "__init__", lambda self: None)
    monkeypatch.setattr(Windows, "logged_in_sessions", lambda self: [(a, i, 999) for i, a in enumerate(accounts)])
    output = io.StringIO()
    status = serve(io.StringIO('{"id":1,"command":"logged_accounts"}\n'), output,
                   session_factory=lambda *a: pytest.fail("account detection cannot open chat data"))
    result = json.loads(output.getvalue())
    assert result["ok"] == ok
    if ok:
        assert status == 0 and result["result"] == {"accounts": accounts}
    else:
        assert status == 2 and result["error"] == "source_identity_changed"


@pytest.mark.parametrize('first', ['account_not_logged_in', 'read_failed', 'same_account'])
def test_unlogged_window_does_not_hide_enrolled_account(monkeypatch, first):
    windows = object.__new__(Windows)
    closed = []
    windows.api = SimpleNamespace(process_ids=lambda: [1, 2], open=lambda pid, _: pid,
        identity=lambda *_: SimpleNamespace(version='synthetic'), close=closed.append)
    windows.modules = lambda pid: {'weixin.dll': (100, 'synthetic.dll')}
    windows.born = lambda handle: handle * 1000
    monkeypatch.setattr('companion_wechat.native.windows.fingerprint', lambda _: {'version': 'synthetic'})

    def account(handle, base):
        if handle == 1 and first != 'same_account':
            raise ProbeFailure(first)
        return 'wxid_expected'

    windows.account = account
    if first == 'account_not_logged_in':
        assert windows.select('wxid_expected') == (2, 2000)
        assert closed == [1, 2]
    else:
        with pytest.raises(ProbeFailure, match='source_not_unique' if first == 'same_account' else first):
            windows.select('wxid_expected')
        assert closed
