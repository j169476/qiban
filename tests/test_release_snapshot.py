"""Release privacy checks exercise actual new stores and exported archives."""
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from bili_assistant.storage import Store as BiliStore
from companion_core.storage import Store as CoreStore

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_snapshot", ROOT / "scripts/prepare_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.mark.parametrize("store_type", [CoreStore, BiliStore])
def test_new_user_has_no_persona_memories_or_visual_identity(tmp_path, store_type):
    store = store_type(tmp_path / "new-user")
    assert store.initialize()["ok"]
    assert release.blank_persona(store.read("persona")["data"])
    assert store.records("memories") == ([], [])
    assert store.records("relationships") == ([], [])


def synthetic_source(tmp_path):
    root = tmp_path / "source"
    for name in release.SOURCE_DIRS:
        (root / name).mkdir(parents=True)
    for name in release.ROOT_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder\n", encoding="utf-8")
    for name in ("src/bili_assistant/defaults/persona.yaml", "src/companion_core/defaults.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    return root


def test_export_excludes_history_local_state_and_personal_material(tmp_path):
    root = synthetic_source(tmp_path)
    for name in (".git/config", ".dev/private.txt", "bili-assistant-data/persona.yaml",
                 "companion-service-data/secrets.json", "assets/avatars/owner.png",
                 "docs/evidence/real-chat.json", "docs/personas/private.yaml",
                 "plugins/bili-assistant/runtime.json", "src/pkg/__pycache__/private.pyc"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE-SYNTHETIC-MARKER", encoding="utf-8")
    output = tmp_path / "release"
    report = release.export(root, output, ("PRIVATE-SYNTHETIC-MARKER",))
    assert report["ok"] and not report["history_included"]
    assert not (output / "repository/.git").exists()
    with zipfile.ZipFile(output / "source.zip") as bundle:
        assert len(bundle.namelist()) == report["files"]
        assert all(b"PRIVATE-SYNTHETIC-MARKER" not in bundle.read(n) for n in bundle.namelist())
    with pytest.raises(release.ReleaseError, match="Output exists"):
        release.export(root, output)


def test_private_marker_fails_without_echoing_value(tmp_path):
    root = synthetic_source(tmp_path)
    (root / "src/leak.py").write_text("private-owner-value", encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="src/leak.py") as result:
        release.audit(root, ("private-owner-value",))
    assert "private-owner-value" not in str(result.value)


def test_nonempty_default_and_unreviewed_binary_block_release(tmp_path):
    root = synthetic_source(tmp_path)
    path = root / "src/companion_core/defaults.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["persona"]["name"] = "Synthetic configured persona"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="persona must be empty"):
        release.audit(root)
    path.write_bytes((ROOT / "src/companion_core/defaults.json").read_bytes())
    (root / "src/private.png").write_bytes(b"synthetic-image")
    with pytest.raises(release.ReleaseError, match="Unreviewed source file"):
        release.audit(root)


def test_repository_is_exportable_with_empty_defaults():
    _, report = release.audit(ROOT)
    assert report["ok"] and report["blank_persona"]
    names = {item["path"] for item in report["manifest"]}
    assert "tests_core/fixtures/retrieval/cases.json" in names
    assert "src/companion_wechat/native/LICENSE.helper" in names
