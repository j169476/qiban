import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

# The probe scripts are executed from their directory, not installed as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'desktop_probe'))

from build_desktop_sources import (  # noqa: E402
    ARCHIVE,
    MANIFEST,
    PUBLISHED_EXE,
    BuildError,
    record,
    verify,
    write_snapshot,
)

WHITELIST = (
    'LICENSE',
    'scripts/desktop_probe/Program.cs',
    'scripts/desktop_probe/Search.cs',
    'scripts/desktop_probe/Qiban.DesktopProbe.csproj',
    'scripts/desktop_probe/packages.lock.json',
    'scripts/desktop_probe/build.ps1',
    'scripts/desktop_probe/build_desktop_sources.py',
    'scripts/desktop_probe/Assets/index.html',
    'scripts/desktop_probe/Assets/navigation.js',
    'scripts/desktop_probe/licenses/WebView2-SDK.txt',
    'src/companion_service/static/app.css',
    'src/companion_service/static/qiban.css',
    'src/companion_service/static/white-dragon-tea.png',
)
NOT_WHITELISTED = (
    'scripts/desktop_probe/notes.log',
    'scripts/desktop_probe/runtime.json',
    'scripts/desktop_probe/installation.json',
    'scripts/desktop_probe/storage-config.json',
    'scripts/desktop_probe/.dev/source.py',
    'scripts/desktop_probe/logs/run.log',
    'scripts/desktop_probe/test-data/questions.json',
    'scripts/desktop_probe/obj/build.dll',
    'scripts/desktop_probe/installation-result.json',
)
SHELL = b'MZ desktop shell published by build.ps1'


@pytest.fixture
def project(tmp_path):
    """A project tree holding the whitelisted sources and files the whitelist must exclude."""
    root = tmp_path / 'project'
    for name in WHITELIST + NOT_WHITELISTED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('content of ' + name, encoding='utf8')
    return root


def published(project, tmp_path, shell=SHELL, managed=True, subscription=False):
    """Run the pre/post pair the way one build.ps1 pipeline does, and return the record."""
    snapshot = tmp_path / 'snapshot.json'
    write_snapshot(project, snapshot)
    app = tmp_path / 'published'
    app.mkdir()
    (app / PUBLISHED_EXE).write_bytes(shell)
    value = record(project, app, PUBLISHED_EXE, app / ARCHIVE, app / MANIFEST,
                   snapshot, managed, subscription)
    return value, app


def test_snapshot_record_and_verify_describe_one_shell(project, tmp_path):
    value, app = published(project, tmp_path)
    assert value['format'] == 1
    assert value['exe'] == {'file': PUBLISHED_EXE,
                            'sha256': hashlib.sha256(SHELL).hexdigest()}
    assert value['source_archive'] == {'file': ARCHIVE,
                                       'sha256': hashlib.sha256((app / ARCHIVE).read_bytes()).hexdigest()}
    assert value['compiled'] == {'managed_application': True, 'subscription_validation': False}
    assert value['signed'] is False
    for row in value['files']:
        assert row['sha256'] == hashlib.sha256((project / row['path']).read_bytes()).hexdigest()
    verify(app, app / PUBLISHED_EXE)
    # The assembled entry point is the same bytes under the name assemble_application gives it.
    (app / PUBLISHED_EXE).rename(app / 'Qiban.exe')
    verify(app, app / 'Qiban.exe')


def test_source_change_during_publish_fails_the_build(project, tmp_path):
    snapshot = tmp_path / 'snapshot.json'
    write_snapshot(project, snapshot)
    app = tmp_path / 'published'
    app.mkdir()
    (app / PUBLISHED_EXE).write_bytes(SHELL)
    (project / 'scripts/desktop_probe/Search.cs').write_text('changed while publishing', encoding='utf8')
    with pytest.raises(BuildError) as failure:
        record(project, app, PUBLISHED_EXE, app / ARCHIVE, app / MANIFEST, snapshot, True, False)
    assert failure.value.code == 'desktop_sources_changed'
    assert 'Search.cs' in str(failure.value)
    assert not (app / MANIFEST).exists() and not (app / ARCHIVE).exists()


def test_inconsistent_shell_or_archive_is_refused(project, tmp_path):
    value, app = published(project, tmp_path)
    (app / PUBLISHED_EXE).rename(app / 'Qiban.exe')
    (app / 'Qiban.exe').write_bytes(SHELL + b' rebuilt elsewhere')
    with pytest.raises(BuildError) as failure:
        verify(app, app / 'Qiban.exe')
    assert failure.value.code == 'desktop_build_exe_mismatch'
    (app / 'Qiban.exe').write_bytes(SHELL)
    verify(app, app / 'Qiban.exe')
    with zipfile.ZipFile(app / ARCHIVE, 'a') as bundle:
        bundle.writestr('scripts/desktop_probe/extra.cs', b'not from the snapshot')
    with pytest.raises(BuildError) as failure:
        verify(app, app / 'Qiban.exe')
    assert failure.value.code == 'desktop_sources_archive'
    # A record that claims a signature nobody produced is not accepted either.
    value['signed'] = True
    (app / MANIFEST).write_text(json.dumps(value), encoding='utf8')
    with pytest.raises(BuildError) as failure:
        verify(app, app / 'Qiban.exe')
    assert failure.value.code == 'desktop_build_signature_claim'


def test_archive_holds_only_whitelisted_sources(project, tmp_path):
    value, app = published(project, tmp_path)
    expected = set(WHITELIST)
    assert {row['path'] for row in value['files']} == expected
    with zipfile.ZipFile(app / ARCHIVE) as bundle:
        assert set(bundle.namelist()) == expected
    recorded = json.loads((app / MANIFEST).read_text(encoding='utf8'))
    assert {row['path'] for row in recorded['files']} == expected
