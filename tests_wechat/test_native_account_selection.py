from types import SimpleNamespace

import pytest

from companion_wechat.native.protocol import ProbeFailure
from companion_wechat.native.windows import Windows


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
