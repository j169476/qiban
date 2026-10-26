"""Offline setup and reconnection tests, never construct Windows()."""
import json
from pathlib import Path

import pytest

from companion_wechat.module import ReconnectingTransport, WeChatModule, configured_module, load_options
from companion_wechat.module_cli import main
from companion_wechat.receiver_transport import WorkerError
from tests_wechat.test_receiver import Worker, snapshot


@pytest.mark.asyncio
async def test_each_reconnect_uses_enrolled_identity_and_fresh_pid():
    selected = []
    worker = Worker([{}, {}])

    def select(account):
        selected.append(account)
        return 100 + len(selected), 500 + len(selected)

    transport = ReconnectingTransport("unused", select=select, worker=worker)
    config = {"client_pid": 999}
    previous = snapshot()
    for _ in range(2):
        await transport.request("open", config=config, **previous)
    assert selected == [previous["enrollment"]["self_username"]] * 2
    assert [c[1]["config"]["client_pid"] for c in worker.calls] == [101, 102]
    assert all(c[1]["checkpoint"] == previous["checkpoint"] for c in worker.calls)
    assert config == {"client_pid": 999}
    await transport.close()
    assert worker.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["client_not_running", "client_identity_changed", "process_closed"])
async def test_read_rechecks_login_without_changing_checkpoint(code):
    now, checked = [0], []
    worker = Worker([{}, {}])

    def check(account, identity):
        checked.append((account, identity))
        raise WorkerError(code)

    transport = ReconnectingTransport("unused", select=lambda _: (101, 501), worker=worker,
                                      check=check, clock=lambda: now[0])
    previous = snapshot()
    await transport.request("open", config={"client_pid": 999}, **previous)
    await transport.request("read", checkpoint=previous["checkpoint"])
    assert not checked
    now[0] = 5
    with pytest.raises(WorkerError) as caught:
        await transport.request("read", checkpoint=previous["checkpoint"])
    assert caught.value.code == code
    assert checked == [(previous["enrollment"]["self_username"], (101, 501))]
    assert [call[0] for call in worker.calls] == ["open", "read"]
    assert worker.calls[1][1]["checkpoint"] == previous["checkpoint"]


@pytest.mark.parametrize("fault,code", [("logout", "client_not_running"),
    ("account", "client_identity_changed"), ("pid", "process_closed"), ("access", "process_access_denied"),
    (None, None)])
def test_fast_client_check_requires_same_login_and_process(monkeypatch, fault, code):
    from types import SimpleNamespace

    from companion_wechat.native.protocol import ProbeFailure

    closed = []

    def open_process(*args):
        if fault == "access":
            raise OSError("synthetic")
        return "synthetic-handle"

    def account(*args):
        if fault == "logout":
            raise ProbeFailure("account_not_logged_in")
        return "other" if fault == "account" else "enrolled"

    windows = SimpleNamespace(api=SimpleNamespace(open=open_process, close=closed.append),
        born=lambda _: 502 if fault == "pid" else 501, modules=lambda _: {"weixin.dll": (100, "unused")},
        account=account)
    monkeypatch.setattr("companion_wechat.native.windows.Windows", lambda: windows)
    if code:
        with pytest.raises(WorkerError) as caught:
            ReconnectingTransport._check_client("enrolled", (101, 501))
        assert caught.value.code == code
    else:
        ReconnectingTransport._check_client("enrolled", (101, 501))
    assert closed == ([] if fault == "access" else ["synthetic-handle"])


def test_missing_module_is_not_registered_and_constructor_is_inert(tmp_path):
    assert configured_module(None, tmp_path, "synthetic") is None
    path = tmp_path / ".dev/wechat/module.json"
    path.parent.mkdir(parents=True)
    path.write_text("invalid configuration", encoding="utf-8")
    module = configured_module(None, tmp_path, "synthetic")
    assert isinstance(module, WeChatModule)
    assert module.delegate is None and module.runtime_status()["state"] == "paused"


def test_setup_and_check_are_offline_and_never_grant_reply_permission(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("companion_wechat.module_cli.validate_build", lambda _: {})
    for name in ("database-locations.json", "test-targets-intent.json", "native-module-build/xln_native_module.dll"):
        path = tmp_path / ".dev/wechat" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
    python = tmp_path / ".dev/wechat-db-venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"synthetic")
    assert main(["configure", "--project", str(tmp_path)]) == 0
    options = load_options(tmp_path)
    assert options["version"] == 2 and "family_enabled" not in options
    assert all(isinstance(options[k], Path) for k in ("locations", "targets", "native_dll", "database_python"))
    assert main(["configure", "--project", str(tmp_path), "--family"]) == 2
    assert main(["configure", "--project", str(tmp_path), "--no-family"]) == 2
    path = tmp_path / ".dev/wechat/module.json"
    legacy = json.loads(path.read_text()) | {"version": 1, "family_enabled": True}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert load_options(tmp_path)["version"] == 1
    assert main(["configure", "--project", str(tmp_path)]) == 0
    assert "family_enabled" not in load_options(tmp_path)
    for line in capsys.readouterr().out.splitlines():
        assert json.loads(line)["messages_sent"] == 0 and not json.loads(line)["client_accessed"]
