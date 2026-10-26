from types import SimpleNamespace

import pytest

from companion_codex.model import CodexModel
from companion_codex.selection import ModelSelection
from companion_core.errors import AssistantError
from tests_postgres.test_web import client, login
from tests_postgres.test_web import service as service


@pytest.fixture
def selection(service, monkeypatch, tmp_path):
    (tmp_path / "config.toml").write_text('model="host-model"\nmodel_reasoning_effort="low"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    models = [{"model": "first-model", "displayName": "First", "defaultReasoningEffort": "medium",
               "supportedReasoningEfforts": [{"reasoningEffort": v} for v in ("low", "medium", "ultra")]},
              {"model": "second-model", "defaultReasoningEffort": "high",
               "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]
    service.host.request = lambda method, params: {"data": models, "nextCursor": None}
    service.control("synthetic-bridge-secret", "admin", "management_open", {})
    return ModelSelection(service.store, service.host)


def change(service, scope="global", model="first-model", effort=None, version=None):
    return service.control("synthetic-bridge-secret", "admin", "model_settings_set", {
        "scope": scope, "model": model, "effort": effort, "expected_version": version})


def test_web_and_chat_share_global_overrides_and_persistence(service, selection):
    assert selection.resolve("qq")["model"] == "host-model"
    global_choice = change(service)
    assert selection.resolve("qq")["effort"] == "medium"
    admin = client(service, "admin")
    headers = login(admin, service, "admin")
    args = {"scope": "qq", "model": "second-model", "effort": "high", "expected_version": global_choice["version"]}
    result = admin.post("/operate", json={"operation": "model_settings_set", "arguments": args}, headers=headers)
    assert result.status_code == 200, result.text
    fresh = ModelSelection(service.store, service.host)
    assert fresh.resolve("qq")["model"] == "second-model"
    assert fresh.resolve("wechat")["model"] == "first-model"
    assert fresh.resolve("bilibili")["source"] == "global"
    cleared = change(service, "qq", None, version=result.json()["version"])
    assert cleared["effective"]["qq"]["source"] == "global"
    assert change(service, model=None, version=cleared["version"])["effective"]["qq"]["model"] == "host-model"


def test_invalid_models_efforts_and_stale_versions_do_not_change_settings(service, selection):
    saved = change(service)
    for kwargs, code in [({"model": "unknown", "version": saved["version"]}, "model_unavailable"),
                         ({"effort": "high", "version": saved["version"]}, "model_effort_invalid"),
                         ({"version": None}, "edit_conflict")]:
        with pytest.raises(AssistantError) as error:
            change(service, **kwargs)
        assert error.value.code == code
        assert selection.read()["version"] == saved["version"]


def test_model_controls_require_management_identity_and_web_csrf(service, selection):
    with pytest.raises(AssistantError) as error:
        service.control("synthetic-bridge-secret", "control", "model_settings_get", {})
    assert error.value.code == "admin_required"
    admin = client(service, "admin")
    login(admin, service, "admin")
    assert admin.post("/operate", json={"operation": "model_catalog", "arguments": {}},
                      headers={"origin": "http://127.0.0.1:45180"}).status_code == 403
    service.host.desktop = SimpleNamespace(status=lambda: {"available": False})
    assert service.control("synthetic-bridge-secret", "admin", "model_settings_get", {})["version"] is None
    with pytest.raises(AssistantError) as error:
        change(service)
    assert error.value.code == "host_stopped"


def test_catalog_paginates_and_rejects_repeated_cursor(service, selection):
    calls = []

    def request(method, args):
        calls.append(args)
        return {"data": [{"model": "one" if "cursor" not in args else "two"}],
                "nextCursor": "next" if "cursor" not in args else None}

    service.host.request = request
    assert len(selection.catalog()["items"]) == 2 and calls[1]["cursor"] == "next"
    service.host.request = lambda *args: {"data": [], "nextCursor": "loop"}
    with pytest.raises(AssistantError) as error:
        selection.catalog()
    assert error.value.code == "model_catalog_invalid"


@pytest.mark.asyncio
async def test_inflight_request_keeps_selection_next_request_uses_new_model(service, selection, monkeypatch):
    change(service)
    seen = []

    async def generate(*args, **kwargs):
        seen.append((kwargs["model"], kwargs["reasoning_effort"]))
        if len(seen) == 1:
            change(service, model="second-model", version=selection.read()["version"])
            assert (kwargs["model"], kwargs["reasoning_effort"]) == ("first-model", "medium")
        return {"ok": True}

    monkeypatch.setattr("companion_codex.app_server.generate", generate)
    model = CodexModel(selection=lambda: selection.resolve("qq"))
    for _ in range(2):
        await model.generate("", {}, {}, store=service.store, policy={}, guard=lambda: None)
    assert seen == [("first-model", "medium"), ("second-model", "high")]
