"""Prepare one isolated *native* upgrade-acceptance fixture; it accepts nothing by itself.

The source-level batches next door drive the upgrade with synthetic program trees. A real
acceptance needs the opposite: a published application made of the frozen shell, the frozen
``Qiban.Storage`` payload and the frozen PostgreSQL payload, one fresh data profile of its own,
and one upgrade already carried through - so that a person can then start the application and go
through the Windows confirmation, the emergency seal and the way back with the real frozen code.

This preparer builds exactly that state and stops. It is deliberately *not* the acceptance:

* it installs no verification channel (``upgrade.verification``) and runs no rollback, so no
  Windows confirmation is faked here and the only way forward is the host's own prompt;
* it never writes outside the one evidence directory it creates, and never touches another
  profile, another installation, the network, a model or a VM;
* it does not delete the credential slots it creates - those are what the person verifying the
  installation will use - and it records their ids (never their values) so a later cleanup knows
  what it may remove.

The two published identities are the *same frozen code*. The difference between the installed
program and the program the transaction is heading for is one text marker
(``synthetic-version.txt``) and one extra published file (``new-only.txt``); the target manifest is
derived from the installed one, so no second full payload is copied. Nothing here claims a
cross-schema upgrade: both identities run the same schema.

Usage (all four paths are required, ``--mode`` defaults to ``normal``)::

    python scripts/desktop_probe/prepare_upgrade_native.py \\
        --evidence .dev/refactor-r0/desktop/upgrade-177/native-01 \\
        --shell <self-contained shell directory> \\
        --storage <frozen resources/storage directory> \\
        --postgres <frozen resources/postgres directory> \\
        --mode normal

The evidence directory must not exist yet and must lie below
``.dev/refactor-r0/desktop/upgrade-177``. Every write of this program stays inside it.

What it costs: the payload is copied twice by design (the installed program, and the maintenance
carrier ``service.prepare`` keeps of it), the profile's cluster is created once, and the published
identity of the whole application is hashed a few times; expect a few minutes and roughly twice
the payload size on disk. Each PostgreSQL server it starts is its own, on this profile's own port,
and is stopped again - a failure still leaves no server running.

The profile and the clone are created by the same source modules the frozen storage payload ships
(``installation.initialize``, ``upgrade.service``); what the person then starts is the frozen
shell from the fixture, which is the part this preparer deliberately does not run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(HERE))

import installation  # noqa: E402
import run_upgrade_source as driver  # noqa: E402
import upgrade.data as data  # noqa: E402
import upgrade.journal as journal  # noqa: E402
import upgrade.program as program  # noqa: E402
import upgrade.service as service  # noqa: E402

AREAS = driver.DEV
RESULT = 'result.json'
VERSION_FILE = 'synthetic-version.txt'
NEW_ONLY = 'new-only.txt'
OLD_VERSION = b'frozen shell: same code, first published identity\n'
NEW_VERSION = b'frozen shell: same code, second published identity\n'
NEW_ONLY_BODY = b'published only by the second identity of the same frozen shell\n'
MARKER = {'persona': '原生验收新版克隆角色', 'memory_id': 'native-acceptance-marker',
          'memory': '原生验收：这一行只应出现在新版克隆资料里'}
STORAGE_EXE = 'Qiban.Storage.exe'
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)


class PreparerError(Exception):
    """A refusal of this preparer itself; the CLI prints the code and the reason."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def refuse(code, message):
    raise PreparerError(code, message)


def _reparse(path):
    try:
        details = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, 'st_file_attributes', 0) & REPARSE)


# -- argument validation ---------------------------------------------------


def evidence_directory(argument):
    """The one directory this run may write: new, empty and below this round's evidence area."""
    try:
        resolved = Path(argument).expanduser().resolve()
    except (OSError, RuntimeError):
        refuse('native_evidence', '证据目录无法解析。')
    if resolved.exists():
        refuse('native_evidence', '证据目录已经存在，请换一个全新的名字。')
    area = AREAS.resolve()
    if resolved == area or not resolved.is_relative_to(area):
        refuse('native_evidence', f'证据目录必须位于 {area} 之下。')
    return resolved


def source_directory(argument, label, needed):
    """A read-only payload this run copies; it must really hold what the label promises."""
    try:
        resolved = Path(argument).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        refuse('native_source', f'{label}目录不可用。')
    if not resolved.is_dir() or _reparse(resolved):
        refuse('native_source', f'{label}目录不可用。')
    for name in needed:
        target = resolved.joinpath(*name.split('/'))
        if not target.is_file() or _reparse(target):
            refuse('native_source', f'{label}目录缺少 {name}。')
    return resolved


# -- writing the fixture ---------------------------------------------------


def digest_file(path):
    number = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            number.update(block)
    return number.hexdigest()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def copy_tree(source, target, *, skip=frozenset()):
    """Copy one published payload; a link or junction is refused instead of followed."""
    source, target = Path(source), Path(target)
    copied = 0
    for entry in sorted(os.scandir(source), key=lambda item: item.name):
        if entry.name in skip:
            continue
        if _reparse(entry.path):
            refuse('native_source', f'来源目录中存在链接或联接：{entry.name}。')
        destination = target/entry.name
        if entry.is_dir(follow_symlinks=False):
            destination.mkdir(parents=True, exist_ok=True)
            copied += copy_tree(Path(entry.path), destination)
        elif entry.is_file(follow_symlinks=False):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(entry.path, destination)
            copied += 1
        else:
            refuse('native_source', f'来源目录中存在特殊条目：{entry.name}。')
    return copied


def app_exe(app):
    """The executable a person starts: the shell's own entry point, whatever it is named."""
    for name in ('Qiban.exe', 'Qiban.DesktopProbe.exe'):
        if (Path(app)/name).is_file():
            return Path(app)/name
    candidates = sorted(path for path in Path(app).glob('*.exe') if path.is_file())
    if not candidates:
        refuse('native_source', 'shell 目录里没有可执行的程序。')
    return candidates[0]


def published_rows(app, *, skip=frozenset()):
    """Every file of the assembled application, relative and with its size and digest."""
    rows, stack = {}, [(Path(app), '')]
    while stack:
        folder, prefix = stack.pop()
        for entry in sorted(os.scandir(folder), key=lambda item: item.name):
            name = prefix+entry.name
            if name in skip:
                continue
            if _reparse(entry.path):
                refuse('native_app', f'组装目录中存在链接或联接：{name}。')
            if entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), name+'/'))
            elif entry.is_file(follow_symlinks=False):
                rows[name] = (entry.stat(follow_symlinks=False).st_size,
                              digest_file(entry.path))
            else:
                refuse('native_app', f'组装目录中存在特殊条目：{name}。')
    return rows


def document_bytes(rows):
    """One published identity document, in the installer's own layout."""
    files = [{'path': path, 'bytes': size, 'sha256': checksum}
             for path, (size, checksum) in sorted(rows.items())]
    value = {'format': 1, 'count': len(files), 'bytes': sum(row['bytes'] for row in files),
             'files': files}
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode('utf8')


def target_rows(rows):
    """The target identity of the same frozen code: one marker replaced, one file added."""
    target = dict(rows)
    target[VERSION_FILE] = (len(NEW_VERSION), digest(NEW_VERSION))
    target[NEW_ONLY] = (len(NEW_ONLY_BODY), digest(NEW_ONLY_BODY))
    return target


def swap_identity(app, rows, identity):
    """Apply the target identity to the installed program and prove the document it reads."""
    app = Path(app)
    (app/VERSION_FILE).write_bytes(NEW_VERSION)
    (app/NEW_ONLY).write_bytes(NEW_ONLY_BODY)
    (app/program.IDENTITY).write_bytes(document_bytes(rows))
    published = program.document(app)
    if published['sha256'] != identity:
        refuse('native_app', '组装后的发布身份与目标清单不符。')
    return {'identity_sha256': published['sha256'], 'count': published['count'],
            'bytes': published['bytes']}


def servers():
    """Every server this run started, so a failure still leaves none of them running."""
    started = []

    def start(root, binaries, port):
        value = driver.Server(root, binaries, port)
        started.append(value)
        return value.start()

    def close():
        for value in reversed(started):
            try:
                value.stop()
            except Exception:  # noqa: BLE001 - the evidence keeps the failure, not a crash here
                pass

    return start, close


def profile_seed(root, app, installation_id, start):
    """The fresh profile's own synthetic content; one server, started and stopped here."""
    state = data.state(root)
    server = start(root, Path(app)/'resources'/'postgres'/'bin', state['port'])
    try:
        return driver.seed(root, installation_id, state['port'])
    finally:
        server.stop()


def mark_clone(root, app):
    """Write the distinguishable name that only the newer generation must carry."""
    with data.session(root, app) as (store, _key, _value, _credential, descriptor):
        version = store.version('persona')
        persona = store.read('persona')['data']
        persona['name'] = MARKER['persona']
        persona['interests'] = [MARKER['memory'], '回退后这一行不应出现在旧代次']
        store.write('persona', persona, version, 'native-acceptance')
        store.write('memories', {'schema_version': 1, 'id': MARKER['memory_id'],
                                 'kind': 'user_preference', 'content': MARKER['memory'],
                                 'source': {'type': 'user_instruction',
                                            'recorded_at': driver.utcnow(),
                                            'reference': 'native-acceptance:marker'},
                                 'confidence': 'explicit', 'tags': ['原生验收'], 'locked': False},
                   None, 'native-acceptance', item_id=MARKER['memory_id'])
        return {'generation': descriptor['generation'], 'persona': persona['name'],
                'memory_id': MARKER['memory_id'], 'memory': MARKER['memory']}


def confirm_transaction(root, app, start):
    """The newer program confirms the pairing over a server this call starts and stops."""
    state = data.state(root)
    server = start(root, Path(app)/'resources'/'postgres'/'bin', state['port'])
    try:
        report = service.confirm(root, app)
    finally:
        server.stop()
    cleanup = report.get('cleanup')
    return {'phase': report['phase'], 'transaction': report['transaction'],
            'cleanup_complete': None if cleanup is None else cleanup.get('complete')}


def damage_backend(app):
    """The fixture's one deliberate break: the newer storage backend cannot start any more."""
    target = Path(app)/'resources'/'storage'/STORAGE_EXE
    if not target.is_file():
        refuse('native_app', '程序目录里没有可供破坏的新版 storage 程序。')
    original = target.stat().st_size
    body = b'# damaged for the native acceptance fixture: this frozen build must not run\n'
    target.write_bytes(body)
    return {'path': 'resources/storage/'+STORAGE_EXE, 'previous_bytes': original,
            'bytes': len(body), 'sha256': digest(body),
            'note': '仅夹具：新版后台已不可运行；维护载体与目标发布清单保持完好。'}


# -- the run ---------------------------------------------------------------


def create_evidence(arguments):
    """Every write of this program happens below this directory, created once."""
    evidence = arguments.evidence
    evidence.mkdir(parents=True)
    (evidence/RESULT).write_text(json.dumps(
        {'format': 1, 'passed': False, 'step': 'started', 'mode': arguments.mode},
        ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    return evidence


def assemble(arguments, app, plan):
    """The published application: the frozen shell, plus the two given resource payloads."""
    shell = arguments.shell
    if (shell/program.IDENTITY).is_file():
        refuse('native_source', 'shell 目录已带发布身份记录，请给未发布的 shell 输出目录。')
    for label in ('storage', 'postgres'):
        if (shell/'resources'/label).exists():
            refuse('native_source', f'shell 目录里已经带有 resources/{label}，请给纯 shell。')
    files = copy_tree(shell, app)
    for label, source in (('storage', arguments.storage), ('postgres', arguments.postgres)):
        target = app/'resources'/label
        target.mkdir(parents=True, exist_ok=True)
        files += copy_tree(source, target)
    app.mkdir(parents=True, exist_ok=True)
    (app/VERSION_FILE).write_bytes(OLD_VERSION)
    rows = published_rows(app, skip={program.IDENTITY})
    identity = document_bytes(rows)
    (app/program.IDENTITY).write_bytes(identity)
    published = program.document(app)
    if published['sha256'] != digest(identity):
        refuse('native_app', '组装后的发布身份记录无法复核。')
    plan['target_rows'] = target_rows(rows)
    return {'published_files': published['count'], 'files': files+2,
            'bytes': published['bytes'], 'exe': str(app_exe(app)),
            'identity_sha256': published['sha256'],
            'target_identity': digest(document_bytes(plan['target_rows']))}


def prepare(arguments, record):
    start, close = servers()
    record['steps'] = []
    created, plan = {'slots': []}, {}

    def attempt(name, function):
        started = time.monotonic()
        try:
            detail = function()
        except BaseException as error:
            code = getattr(error, 'code', type(error).__name__)
            record['steps'].append({'step': name, 'ok': False, 'error': str(code),
                                    'reason': str(error)[:400],
                                    'seconds': round(time.monotonic()-started, 3)})
            raise
        record['steps'].append({'step': name, 'ok': True,
                                'seconds': round(time.monotonic()-started, 3),
                                'detail': detail if isinstance(detail, (dict, list)) else None})
        return detail

    try:
        evidence = attempt('evidence', lambda: create_evidence(arguments))
        record.update({'evidence': str(evidence), 'mode': arguments.mode})
        app, root = evidence/'App', evidence/'Profile'
        binaries = app/'resources'/'postgres'/'bin'
        worker = app/'resources'/'storage'/STORAGE_EXE

        executed = attempt('assemble', lambda: assemble(arguments, app, plan))
        record['app'] = {'path': str(app), 'files': executed['files'],
                         'exe': str(executed['exe'])}

        profile = attempt('install_profile', lambda: installation.initialize(
            root, binaries, worker, None))
        installation_id = profile['installation']
        created['slots'].append(installation_id)
        record.update({'installation': installation_id, 'root': str(root), 'installed': str(app)})
        record['launch_recovery'] = [str(executed['exe']), '--upgrade-recovery', str(root), str(app)]
        record['launch'] = list(record['launch_recovery'])

        record['seeded'] = attempt('seed', lambda: profile_seed(
            root, app, installation_id, start))

        prepared = attempt('prepare', lambda: service.prepare(
            root, app, target_identity=executed['target_identity']))
        record.update({'carrier': str(program.tree(root, prepared['carrier'])),
                       'transaction': prepared['transaction'],
                       'clone': prepared['generation']})
        created['slots'].append(prepared['generation'])

        record['identity'] = attempt('swap_identity', lambda: swap_identity(
            app, plan['target_rows'], executed['target_identity']))
        attempt('initialize_clone', lambda: installation.initialize(
            root, binaries, worker, None))
        record['marker'] = attempt('mark_clone', lambda: mark_clone(root, app))
        record['confirm'] = attempt('confirm', lambda: confirm_transaction(root, app, start))
        if arguments.mode == 'emergency':
            record['damage'] = attempt('damage_backend', lambda: damage_backend(app))
        record['identities'] = {'installed_before': executed['identity_sha256'],
                                'installed_after': executed['target_identity'],
                                'carrier': prepared['carrier']}
        record['selection'] = journal.selection(data.selected(root, installation_id))
        record['fixture'] = {'same_frozen_code': True, 'cross_schema': False,
                             'real_accounts': False,
                             'difference': [VERSION_FILE, NEW_ONLY]}
        return record
    finally:
        close()
        record['credentials'] = {'slots': sorted(set(created['slots'])),
                                 'values_written': False}


def result(record, error=None):
    value = {**record, 'format': 1, 'passed': error is None, 'finished_at': driver.utcnow()}
    if error is not None:
        value['failed_step'] = record['steps'][-1]['step'] if record.get('steps') else 'unknown'
        value['error'] = str(getattr(error, 'code', type(error).__name__))
        value['reason'] = str(error)[:400]
    return value


def write_result(evidence, value):
    try:
        (Path(evidence)/RESULT).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)+'\n', encoding='utf8')
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--evidence', required=True,
                        help='全新的证据目录，必须位于 .dev/refactor-r0/desktop/upgrade-177 之下')
    parser.add_argument('--shell', required=True, help='自包含 shell 输出目录（未发布）')
    parser.add_argument('--storage', required=True, help='冻结的 resources/storage 目录')
    parser.add_argument('--postgres', required=True, help='冻结的 resources/postgres 目录')
    parser.add_argument('--mode', choices=('normal', 'emergency'), default='normal',
                        help='emergency 在确认后破坏夹具里的新版 storage 程序；normal 保持完好')
    arguments = parser.parse_args(argv)
    record, failure = {}, None
    try:
        arguments.evidence = evidence_directory(arguments.evidence)
        arguments.shell = source_directory(arguments.shell, 'shell', ())
        arguments.storage = source_directory(arguments.storage, 'storage', (STORAGE_EXE,))
        arguments.postgres = source_directory(arguments.postgres, 'postgres',
                                              ('bin/pg_ctl.exe', 'bin/postgres.exe'))
        record['sources'] = {'shell': str(arguments.shell), 'storage': str(arguments.storage),
                             'postgres': str(arguments.postgres)}
        prepare(arguments, record)
    except BaseException as error:  # noqa: BLE001 - the failure itself is the evidence
        failure = error
    created = isinstance(getattr(arguments, 'evidence', None), Path) and \
        Path(arguments.evidence).is_dir()
    if failure is not None:
        if created:
            write_result(arguments.evidence, result(record, failure))
        print(json.dumps({'passed': False,
                          'code': str(getattr(failure, 'code', type(failure).__name__)),
                          'reason': str(failure)[:400]}, ensure_ascii=False))
        return 2 if isinstance(failure, PreparerError) else 1
    write_result(arguments.evidence, result(record))
    print(json.dumps({'passed': True, 'mode': arguments.mode,
                      'evidence': str(arguments.evidence),
                      'root': record.get('root'), 'installed': record.get('installed'),
                      'carrier': record.get('carrier'),
                      'launch': record.get('launch_recovery')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
