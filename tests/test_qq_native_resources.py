import hashlib

import pytest

from companion_core.errors import AssistantError
from companion_qq import native_resources as resources


@pytest.fixture
def files(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "pack"
    source.mkdir()
    entries = []
    for name in ("node.exe", "wrapper.node", "QQNT.dll"):
        content = name.encode()
        (source / name).write_bytes(content)
        entries.append({"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
    monkeypatch.setattr(resources, "runtime_lock", lambda: {"node": "test", "qq": "test", "files": entries})
    return source, target


def test_only_locked_resources_are_copied_never_database_or_login_data(files):
    source, target = files
    for name in ("guild1.db", "login.json", "beacon_report.log"):
        (source / name).write_text("private", encoding="utf8")
    result = resources.prepare_local_runtime(source, target)
    assert result["files"] == 3
    assert {p.name for p in target.iterdir()} == {"node.exe", "wrapper.node", "QQNT.dll"}
    assert resources.prepare_local_runtime(source, target) == result


def test_partial_copy_is_not_launchable_and_explicit_retry_completes(files, monkeypatch):
    source, target = files
    original = resources.shutil.copyfileobj

    def interrupted(incoming, outgoing):
        outgoing.write(incoming.read(2))
        raise OSError("test copy interruption")

    with monkeypatch.context() as patch:
        patch.setattr(resources.shutil, "copyfileobj", interrupted)
        with pytest.raises(OSError):
            resources.prepare_local_runtime(source, target)
    assert any(p.suffix == ".part" for p in target.iterdir())
    with pytest.raises(AssistantError):
        resources.verify_runtime(target)
    assert resources.shutil.copyfileobj == original
    assert resources.prepare_local_runtime(source, target)["files"] == 3


def test_corruption_and_extra_state_block_launch(files):
    source, target = files
    resources.prepare_local_runtime(source, target)
    (target / "node.exe").write_bytes(b"node.exE")
    with pytest.raises(AssistantError):
        resources.verify_runtime(target)
    resources.prepare_local_runtime(source, target)
    (target / "guild1.db").write_bytes(b"state")
    with pytest.raises(AssistantError):
        resources.verify_runtime(target)
    with pytest.raises(AssistantError):
        resources.prepare_local_runtime(source, target)


def test_data_is_separate_from_runtime(files, tmp_path):
    source, target = files
    resources.prepare_local_runtime(source, target)
    for data in (target, target / "data", tmp_path):
        with pytest.raises(AssistantError):
            resources.working_directory(target, data)
    working = resources.working_directory(target, tmp_path / "account")
    assert working == tmp_path / "account" / "work" and working.is_dir()


def test_mismatching_source_is_not_installed(files):
    source, target = files
    (source / "node.exe").write_bytes(b"wrong version")
    with pytest.raises(AssistantError):
        resources.prepare_local_runtime(source, target)
    assert not (target / "node.exe").exists()


def test_execution_copies_keep_observed_kernel_log_out_of_pack(files, tmp_path):
    source, target = files
    resources.prepare_local_runtime(source, target)
    execution, working = resources.execution_runtime(target, tmp_path / "account")
    assert execution.parent == working.parent and execution != target
    (execution / "beacon_report.log").write_text("kernel output", encoding="utf8")
    assert resources.execution_runtime(target, tmp_path / "account") == (execution, working)
    assert resources.verify_runtime(target)["files"] == 3
    (execution / "unapproved.dll").write_bytes(b"other binary")
    with pytest.raises(AssistantError):
        resources.execution_runtime(target, tmp_path / "account")
