import json

import pytest

from companion_core.errors import AssistantError
from companion_service.installation import Journal, finalize


def test_failed_installation_retains_identifier_and_cannot_be_overwritten(tmp_path):
    journal = Journal(tmp_path)
    journal.begin("a" * 32, "synthetic-source-fingerprint")
    journal.advance("directories")
    journal.failed("database_init")
    assert journal.status()["stage"] == "directories"
    assert journal.status()["error"] == "database_init"
    assert not journal.status()["can_finalize"]
    with pytest.raises(AssistantError):
        journal.begin("b" * 32, None)
    with pytest.raises(AssistantError):
        finalize(tmp_path)
    with pytest.raises(AssistantError):
        journal.advance("reserved")
    assert journal.read()["id"] == "a" * 32


def test_recovery_rejects_tampered_scope_before_vault_or_database(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    journal = Journal(tmp_path)
    journal.begin("a" * 32, None)
    journal.advance("verified", config={"id": "b" * 32})
    with pytest.raises(AssistantError) as error:
        finalize(tmp_path)
    assert error.value.code == "installation_journal"
    assert not (tmp_path / ".companion-service.json").exists()
    state = journal.read()
    state["stage"] = "invalid"
    journal.path.write_text(json.dumps(state))
    with pytest.raises(AssistantError):
        journal.status()


def test_changed_legacy_source_cannot_finalize_an_old_import(tmp_path, monkeypatch):
    from companion_core import legacy_import

    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(legacy_import, "capture", lambda *args: {"fingerprint": "new-source"})
    journal = Journal(tmp_path)
    journal.begin("a" * 32, "old-source")
    journal.advance("verified", config={"id": "a" * 32, "project": str(tmp_path.resolve()),
        "data": str(tmp_path / "companion-service-data"),
        "postgres_runtime": str(local / "Companion" / ("a" * 32)), "database": {"host": "127.0.0.1"}})
    with pytest.raises(AssistantError) as error:
        finalize(tmp_path)
    assert error.value.code == "source_changed"
    assert not (tmp_path / ".companion-service.json").exists()
