from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from companion_core.reply_audience import Audience
from companion_qq import config
from companion_qq.module import QQModule
from companion_qq.native_provider import NativeProvider


@pytest.fixture
def options(tmp_path):
    return {"version": 2, "transport": "native", "account": "10001", "uid": "u_self",
            "private_targets": ["10002"], "groups": ["10003"],
            "runtime": str(tmp_path / "runtime"), "data": str(tmp_path / "account")}


def test_native_config_constructs_owned_provider_without_onebot_credentials(options, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "token", lambda *args: (_ for _ in ()).throw(AssertionError("No OneBot token")))
    consulted = []

    def state(key, default=None, db=None):
        # A fresh profile stores no reply-audience opt-in for either category.
        consulted.append(key)
        return default

    module = QQModule(SimpleNamespace(state=state), tmp_path, options=options)
    assert module.bridge is None
    provider = module.factory()
    assert isinstance(provider, NativeProvider)
    assert provider.config["account"] == {"uin": "10001", "uid": "u_self"}
    assert provider.worker.process is None
    assert sorted(consulted) == sorted(Audience("qq", "10001", kind).key for kind in ("private", "group"))
    assert provider.follow == {}  # No follow-all grant, so only the saved manual targets poll.
    provider.close()


@pytest.mark.parametrize("change", [
    {"transport": "onebot"}, {"http": "http://127.0.0.1:3000"}, {"launch": {}},
    {"uid": ""}, {"data": "relative"}, {"private_targets": ["10001"]}, {"groups": ["10003", "10003"]},
])
def test_native_configuration_rejects_mixed_transports_and_invalid_identity(options, change):
    with pytest.raises(AssistantError):
        config.validate({**options, **change})
