"""Release privacy checks exercise actual new stores and exported archives."""
import importlib.util
import json
import os
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement

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
                 "docs/evidence/real-chat.json", "docs/personas/private.yaml", "docs/design/美术素材/原始文件索引.tsv",
                 "docs/V8.2-manual-results.csv", "docs/V8.2-evidence-20261026/real-chat.json",
                 "docs/V8.2-bug-diagnosis-20261026.txt", "docs/V8.2-browser-test-report-20261026.txt",
                 "docs/V8.2-repair-progress-20261027.md",
                 "docs/V8.2-development.md", "docs/V0_9_DEVELOPMENT.md",
                 "docs/QIBAN_STANDALONE_ACCEPTANCE.md", "docs/QIBAN_R8_INTERNAL_RECORD.md",
                 "scripts/desktop_probe/obj/project.assets.json",
                 "scripts/desktop_probe/obj/Release/probe.assets.cache",
                 "scripts/desktop_probe/bin/Qiban.DesktopProbe.dll",
                 "plugins/qiban/runtime.json", "src/pkg/__pycache__/private.pyc"):
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
    for relative in ("src/private.png", "src/extra_probe.iss", "src/extra_probe.c",
                     "src/companion_qq/native/LICENSE.Unreviewed"):
        unreviewed = root / relative
        unreviewed.parent.mkdir(parents=True, exist_ok=True)
        unreviewed.write_bytes(b"confirmed text at an unconfirmed path\n")
        with pytest.raises(release.ReleaseError, match="Unreviewed source file"):
            release.audit(root)
        unreviewed.unlink()


def test_repository_is_exportable_with_empty_defaults():
    _, report = release.audit(ROOT)
    assert report["ok"] and report["blank_persona"]
    names = {item["path"] for item in report["manifest"]}
    assert "tests_core/fixtures/retrieval/cases.json" in names
    assert "src/companion_wechat/native/LICENSE.helper" in names
    assert set(release.REVIEWED_TEXT) | set(release.REVIEWED_ASSETS) <= names
    assert {"platforms/linux/README.md", "platforms/linux/server/sshd_config",
            "platforms/linux/core/Dockerfile.dockerignore", "platforms/linux/core/core_acceptance.py",
            "platforms/windows-remote/build.ps1", "platforms/windows-remote/verify.py",
            "tests_postgres/test_windows_generic_actions.py", "tests_postgres/matter_fixture.py",
            "src/companion_core/sql/019_role_reminders.sql", "src/companion_core/sql/024_role_activity.sql"} <= names
    assert not any(name.startswith(release.PRIVATE_PREFIXES) for name in names)
    assert not any(name.startswith(release.LOCAL_BUILD_PREFIXES) for name in names)


def test_git_discovers_platform_source_while_export_requires_review(tmp_path):
    root = synthetic_source(tmp_path)
    (root / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    public = sorted(name for name in release.ROOT_FILES if name.startswith("platforms/"))
    unreviewed = ["platforms/linux/server/new_feature.py", "platforms/windows-remote/Display/NewView.cs",
                  "platforms/linux/new_package/tests/test_new.py", "platforms/experimental/README.md"]
    private = [
        "platforms/linux/core/endpoint.json", "platforms/linux/server/keys/id_ed25519",
        "platforms/linux/server/acceptance-results.json",
        "platforms/linux/server/handoff.md", "platforms/linux/server/sshd_config.local",
        "platforms/windows-remote/Display/display.json", "platforms/windows-remote/build/console.exe",
        "platforms/windows-remote/vendor/QQNT.node",
        "scripts/private/runtime/README.md", "scripts/obj/project.assets.json", "tests/endpoint.json",
        "src/module/data.sqlite-wal", "scripts/node_modules/vendor/package.json",
        "scripts/handoff/checkpoint.md", "tests/.ssh/config", "plugins/qiban/bin/runtime.dll",
        "scripts/host/known_hosts", "scripts/host/ssh_host_ed25519_key", "scripts/engine.node",
    ]
    for name in private + unreviewed:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SYNTHETIC-LOCAL-ONLY", encoding="utf8")
    # Exercise the real ignore engine in a disposable, empty repository.
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    result = subprocess.run(["git", "-c", f"core.excludesFile={os.devnull}", "check-ignore",
                             "--no-index", "-z", "--stdin"], cwd=root, check=True, capture_output=True,
                            input=("\0".join(public + unreviewed + private) + "\0").encode())
    assert set(result.stdout.decode().rstrip("\0").split("\0")) == set(private)
    output = tmp_path / "release"
    report = release.export(root, output, ("SYNTHETIC-LOCAL-ONLY",))
    with zipfile.ZipFile(output / "source.zip") as archive:
        exported = {entry.removeprefix("repository/") for entry in archive.namelist()}
    assert exported == {item["path"] for item in report["manifest"]}
    assert set(public) <= exported and not exported.intersection(private + unreviewed)


@pytest.mark.parametrize("connector", ["companion-qq", "companion-wechat"])
def test_connector_dependencies_accept_the_shipped_core(connector):
    core = tomllib.loads((ROOT / "packages/companion-core/pyproject.toml").read_text(encoding="utf-8"))
    package = tomllib.loads((ROOT / "packages" / connector / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(item) for item in package["project"]["dependencies"]]
    core_requirements = [item for item in requirements if item.name == "local-companion-core"]
    assert len(core_requirements) == 1
    assert core["project"]["version"] in core_requirements[0].specifier


def test_only_public_documents_are_exported(tmp_path):
    root = synthetic_source(tmp_path)
    for name in ('unreviewed.csv', '交接计划.md', 'benchmarks/trials.json'):
        file = root / 'docs' / name
        file.parent.mkdir(exist_ok=True)
        file.write_text('private synthetic content', encoding='utf-8')
    payload, _ = release.audit(root, ('private synthetic content',))
    assert {p for p in payload if p.startswith('docs/')} == {p for p in release.ROOT_FILES if p.startswith('docs/')}


def test_public_attribution_does_not_allow_other_private_content(tmp_path):
    root = synthetic_source(tmp_path)
    approved = release.PUBLIC_TEXT[0]
    marker = approved.split("和")[0]
    (root / "README.md").write_text(approved, encoding="utf-8")
    assert release.audit(root, (marker,))[1]["ok"]
    (root / "docs/桌面声明.md").write_text(approved, encoding="utf-8")
    browser_check = root / "scripts/desktop_probe/check_workspace_browser.cjs"
    browser_check.parent.mkdir(parents=True, exist_ok=True)
    browser_check.write_text("开发者：" + approved, encoding="utf-8")
    assert release.audit(root, (marker,))[1]["ok"]
    (root / "src/private.py").write_text(approved, encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="src/private.py"):
        release.audit(root, (marker,))
    (root / "src/private.py").write_text("", encoding="utf-8")
    (root / "README.md").write_text(approved + "\n" + marker, encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="README.md"):
        release.audit(root, (marker,))


@pytest.mark.parametrize("relative", ["scripts/source_probe" + suffix for suffix in (".cjs", ".cs", ".csproj")]
                         + sorted(release.REVIEWED_TEXT))
def test_probe_source_is_exported_and_keeps_text_privacy_checks(tmp_path, relative):
    root = synthetic_source(tmp_path)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("module.exports = {};\n", encoding="utf-8")
    payload, report = release.audit(root)
    assert report["ok"] and payload[relative] == b"module.exports = {};\n"
    path.write_text("// synthetic-private-value\n", encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="Private marker"):
        release.audit(root, ("synthetic-private-value",))
    path.write_bytes(b"binary\x00data")
    with pytest.raises(release.ReleaseError, match="Binary source file"):
        release.audit(root)
