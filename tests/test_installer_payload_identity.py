"""Offline checks for the installer's complete payload identity and its same-build gate.

Nothing here compiles an installer, launches one, or touches any installed application: the
fixture is a minimal payload that exists only under ``tmp_path`` and that every existing
``check_payload`` rule already accepts. What is checked is that the identity document is
deterministic, covers the whole published file set (so a component-only rebuild is a different
build while the shell record stays identical), and that the script and the compiler command are
wired to exactly that digest.
"""
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

# The probe scripts are executed from their directory, not installed as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'desktop_probe' / 'installer'))

import build_installer as installer  # noqa: E402

SHELL = b'MZ qiban desktop shell fixture\n'
SOURCE = b'// desktop shell source fixture\n'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_component(directory, entrypoint, files):
    """One resource package: the manifest plus exactly the files it lists, nothing else."""
    rows = []
    for name, data in sorted(files.items()):
        path = directory/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        rows.append({'path': name, 'sha256': digest(data)})
    manifest = {'format': 1, 'entrypoint': entrypoint, 'files': rows}
    (directory/installer.MANIFEST_NAME).write_text(json.dumps(manifest), encoding='utf8')


def write_shell_records(root):
    """``desktop-build.json`` and its archive, exactly as build_desktop_sources.verify expects."""
    archive = root/'desktop-sources.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('Program.cs', SOURCE)
    value = {'format': 1,
             'exe': {'file': installer.APP_EXE, 'sha256': digest(SHELL)},
             'source_archive': {'file': archive.name, 'sha256': digest(archive.read_bytes())},
             'files': [{'path': 'Program.cs', 'sha256': digest(SOURCE)}],
             'compiled': {'managed_application': True, 'subscription_validation': False},
             'signed': False}
    (root/installer.MANIFEST).write_text(json.dumps(value), encoding='utf8')


@pytest.fixture
def payload(tmp_path):
    """A minimal assembled application directory that passes every existing check_payload rule."""
    root = tmp_path/'app'
    root.mkdir()
    generated = {installer.APP_EXE, installer.MANIFEST, 'desktop-sources.zip', installer.MANAGED}
    for name in sorted(installer.ROOT_FILES - generated):
        (root/name).write_bytes(f'fixture {name}\n'.encode('utf8'))
    (root/installer.APP_EXE).write_bytes(SHELL)
    (root/installer.MANAGED).write_text('{"mode": "managed", "format": 1}', encoding='utf8')
    write_shell_records(root)
    write_component(root/'resources'/'storage', 'Qiban.Storage.exe',
                    {'Qiban.Storage.exe': b'storage worker fixture\n'})
    write_component(root/'resources'/'postgres', 'bin/postgres.exe',
                    {'bin/postgres.exe': b'postgres fixture\n', 'bin/pg_ctl.exe': b'pg_ctl fixture\n'})
    return root


def identity_of(payload):
    """The identity document exactly as the installer would write it, plus its digest."""
    _, summary, identity = installer.check_payload(payload)
    document = installer.identity_document(identity)
    return summary, identity, document, hashlib.sha256(document).hexdigest()


def test_identity_is_deterministic_sorted_and_covers_every_published_file(payload):
    summary, identity, document, _ = identity_of(payload)
    _, _, again, _ = identity_of(payload)
    assert document == again, 'the same payload must always produce the same identity bytes'
    paths = [row['path'] for row in identity['files']]
    assert paths == sorted(paths)
    assert len(set(paths)) == len(paths) == identity['count'] == summary['files']
    assert identity['bytes'] == summary['bytes']
    published = {path.relative_to(payload).as_posix() for path in payload.rglob('*') if path.is_file()}
    assert set(paths) == published, 'every published file, including each component manifest'
    assert installer.IDENTITY not in set(paths), 'the metadata never lists itself'
    for row in identity['files']:
        assert row['bytes'] == (payload/row['path']).stat().st_size
        assert row['sha256'] == digest((payload/row['path']).read_bytes())


def test_rebuilt_component_is_a_different_identity_with_the_same_shell_record(payload):
    """The 174/175 case: only resources/postgres was rebuilt, the shell was not."""
    before, first, _, before_digest = identity_of(payload)
    write_component(payload/'resources'/'postgres', 'bin/postgres.exe',
                    {'bin/postgres.exe': b'postgres fixture rebuilt\n',
                     'bin/pg_ctl.exe': b'pg_ctl fixture\n',
                     'bin/pg_dump.exe': b'pg_dump fixture\n'})
    after, second, _, after_digest = identity_of(payload)
    # What the previous gate compared is unchanged, so it could not tell the two builds apart.
    assert before['build_record_sha256'] == after['build_record_sha256']
    assert before['exe_sha256'] == after['exe_sha256']
    assert before['files'] + 1 == after['files']
    assert before_digest != after_digest
    changed = {row['path'] for row in first['files']} ^ {row['path'] for row in second['files']}
    assert changed == {'resources/postgres/bin/pg_dump.exe'}
    rows = {row['path']: row['sha256'] for row in second['files']}
    assert rows['resources/postgres/bin/postgres.exe'] == digest(b'postgres fixture rebuilt\n')


def test_notes_and_licence_files_belong_to_the_identity(payload):
    """No manifest covers README or a licence, so only the identity can tell those apart."""
    _, first, _, first_digest = identity_of(payload)
    count = first['count']
    (payload/'README.txt').write_bytes('changed readme\n'.encode('utf8'))
    readme_summary, readme, _, readme_digest = identity_of(payload)  # still a valid payload
    assert readme_summary['files'] == count == readme['count'], 'a changed note is not a new file'
    assert readme_digest != first_digest
    (payload/'Qiban-LICENSE.txt').write_bytes('changed licence\n'.encode('utf8'))
    _, licence, _, licence_digest = identity_of(payload)
    assert licence_digest != readme_digest and licence_digest != first_digest
    rows = {row['path']: row for row in licence['files']}
    assert rows['Qiban-LICENSE.txt']['sha256'] == digest(b'changed licence\n')
    assert rows['README.txt']['sha256'] == digest(b'changed readme\n')


def test_identity_document_holds_no_absolute_path_no_clock_and_no_version(payload):
    _, identity, document, _ = identity_of(payload)
    text = document.decode('utf8')
    assert set(identity) == {'format', 'count', 'bytes', 'files'}
    for row in identity['files']:
        assert set(row) == {'path', 'bytes', 'sha256'}
        assert not Path(row['path']).is_absolute()
        assert not row['path'].startswith('/')
        assert ':' not in row['path'] and '\\' not in row['path']
        assert '..' not in Path(row['path']).parts
    # Neither this machine's temporary path nor the repository's own location leaks into it.
    assert str(payload) not in text and str(installer.REPO) not in text


def test_check_only_reports_the_identity_digest_and_writes_nothing(payload, tmp_path, capsys):
    output = tmp_path/'out'
    arguments = ['--app', str(payload), '--check-only', '--output-dir', str(output)]
    assert installer.main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    _, identity, _, expected = identity_of(payload)
    assert not output.exists(), '--check-only must not create the identity or the output directory'
    assert first['compiled'] is False
    assert first['identity_file'] == installer.IDENTITY
    assert first['identity_sha256'] == expected
    assert first['payload_files'] == identity['count'] and first['payload_bytes'] == identity['bytes']
    assert first['build_record_sha256'] != first['identity_sha256']
    assert installer.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)['identity_sha256'] == expected


def test_compile_setup_passes_both_identity_macros(payload, tmp_path, monkeypatch):
    iscc = tmp_path/'ISCC.exe'
    iscc.write_bytes(b'MZ compiler fixture\n')
    output = tmp_path/'out'
    identity_file = output/installer.IDENTITY
    identity_file.parent.mkdir(parents=True)
    identity_file.write_bytes(b'{}\n')
    commands = []

    def fake_run(command, **kwargs):
        commands.append([str(part) for part in command])
        (output/'Qiban-Setup-0.0.1.exe').write_bytes(b'setup fixture\n')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(installer.subprocess, 'run', fake_run)
    setup = installer.compile_setup(iscc, installer.SCRIPT, output, payload, '0.0.1',
                                    'a'*64, identity_file, 'b'*64)
    assert setup == output/'Qiban-Setup-0.0.1.exe'
    command = commands[0]
    expected_path = str(Path(identity_file).resolve()).rstrip('\\/')
    assert f'--define=PayloadIdentity={expected_path}' in command
    assert f'--define=ExpectedPayloadIdentitySha256={"b"*64}' in command
    assert f'--define=ExpectedBuildRecordSha256={"a"*64}' in command
    assert any(part.startswith('--define=PayloadDir=') for part in command)


def test_install_script_requires_the_identity_and_installs_it_into_the_application():
    script = installer.SCRIPT.read_text(encoding='utf8')
    assert '#error PayloadIdentity is required' in script
    assert '#error ExpectedPayloadIdentitySha256 is required' in script
    assert '#define PayloadIdentityName "qiban-payload.json"' in script
    assert 'Source: "{#PayloadIdentity}"; DestDir: "{app}"; DestName: "{#PayloadIdentityName}"' in script
    assert 'if CompareText(GetSHA256OfFile(IdentityPath), ExpectedIdentitySha256) <> 0 then' in script
    assert 'Result := MissingIdentityRefusal' in script
    # UPGRADE-177: another build is no longer refused unconditionally. It goes through the embedded
    # maintenance glue, which only the installed build itself can turn into a paired upgrade.
    assert 'Result := PrepareUpgrade(Target)' in script
    # The old shell-record rule survives unchanged after the identity comparison.
    assert 'if CompareText(GetSHA256OfFile(RecordPath), ExpectedRecordSha256) <> 0 then' in script


def test_tampering_without_rebuilding_the_manifest_never_becomes_a_new_build(payload):
    component = payload/'resources'/'postgres'
    (component/'bin/pg_ctl.exe').write_bytes(b'tampered fixture\n')
    with pytest.raises(installer.BuildError) as failure:
        installer.check_payload(payload)
    assert failure.value.code == 'installer_resource_mismatch'
    (component/'bin/pg_ctl.exe').write_bytes(b'pg_ctl fixture\n')
    installer.check_payload(payload)
    (component/'bin/extra.dll').write_bytes(b'unregistered fixture\n')
    with pytest.raises(installer.BuildError) as failure:
        installer.check_payload(payload)
    assert failure.value.code == 'installer_resource_unexpected'
