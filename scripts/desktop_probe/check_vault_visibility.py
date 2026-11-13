"""Instrumented, evidence-preserving reproduction of the desktop credential-slot loss.

Background this script answers: the 4-process x 16-slot block of ``check_vault_concurrency.py``
(``section_parallel``) lost 2-4 reachable credential slots in three observed runs on this machine
(13/16, 14/16, 12/16) while every writer process exited 0 and reported its four writes. A later
80 ms critical-section probe showed no overlap between four processes, but that was a different
run: it cannot say whether the failing run itself had overlapping holders. This script therefore
reproduces the same write pattern while recording, from inside the *real* ``VaultCredentials``
calls, everything needed to decide where a slot went.

What is recorded for every round

* Windows session id, active console session, elevation, logon authentication id (the credential
  vault partition), interpreter and library files, and the credential binding keyring uses - for
  this process and for every child, so no run is assumed to share a namespace with another.
* The mutex name each child derives for the round's service, so a name mismatch cannot hide.
* Every low-level keyring step: shared-target read (with the synthetic UserName it returned),
  compound backup write and shared-target write, each with ``perf_counter`` timestamps taken
  inside the real critical section. ``perf_counter`` is the machine-wide Windows performance
  counter, so intervals from different processes are comparable.
* The actual lock hold interval of every single call, recorded by a pass-through wrapper around
  ``credential_vault.exclusive`` that adds no sleep, no retry and no extra read.
* The same-process read-back after every write (presence and a boolean match only).
* A final validation of all slots by one further reader process.

The analyser replays the round and reports three decisive facts: whether the real lock intervals
overlapped, whether a shared read observed something other than what the previous write left
(``stale``), and which slot was overwritten without the compound backup that must preserve it.
A slot disappears exactly when that backup is missing, so the analyser's list is compared with
the reader's.

Cleanup and privacy: one random synthetic service per round; only that round's registered slots
are deleted, and only through the same wrapper. ``CredEnumerate`` is never used, and no other
service, installation or credential is read, written or deleted. Values are synthetic, derived
from a random run token, and are never printed - the report carries booleans, counts, slot
identifiers and file paths only.

Evidence is written under ``<repo>/.dev/refactor-r0/desktop/vault-166-diagnostic``:
``environment.json``, one directory per round (child traces, child stderr, child exit codes,
reader result, ``summary.json``), ``report.json`` and ``transcript.txt``. The first failing round
preserves everything and stops the run. If all rounds pass, the script says that explicitly and
claims no cause.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve()
REPOSITORY = SCRIPT.parents[2]
# A standalone start without PYTHONPATH (for example with -E) still has to import the probe
# modules and companion_core, so the source root comes before any project import below.
sys.path.insert(0, str(REPOSITORY / 'src'))

SERVICE_PREFIX = 'qiban-desktop-probe-r7-visible-'
EVIDENCE = REPOSITORY / '.dev' / 'refactor-r0' / 'desktop' / 'vault-166-diagnostic'
TOKEN_VARIABLE = 'QIBAN_R7_VISIBILITY_TOKEN'
MAX_ROUNDS = 10
WRITERS = 4
SLOTS_PER_WRITER = 4
START_DELAY = 3.0
TIMEOUT = 180


def synthetic(token, slot):
    """The synthetic value of one slot; deterministic, never printed."""
    return hashlib.sha256((token + '|' + slot).encode('utf8')).hexdigest()


# -- environment facts ---------------------------------------------------------
class TokenStatistics(ctypes.Structure):
    _fields_ = [('TokenId', ctypes.c_ulonglong), ('AuthenticationId', ctypes.c_ulonglong),
                ('ExpirationTime', ctypes.c_ulonglong), ('TokenType', ctypes.c_int),
                ('ImpersonationLevel', ctypes.c_int), ('DynamicCharged', ctypes.c_ulong),
                ('DynamicAvailable', ctypes.c_ulong), ('GroupCount', ctypes.c_ulong),
                ('PrivilegeCount', ctypes.c_ulong), ('ModifiedId', ctypes.c_ulonglong)]


def _session_id():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.ProcessIdToSessionId.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    kernel.ProcessIdToSessionId.restype = ctypes.c_int
    kernel.WTSGetActiveConsoleSessionId.restype = ctypes.c_ulong
    session = ctypes.c_ulong()
    ok = kernel.ProcessIdToSessionId(ctypes.c_ulong(os.getpid()), ctypes.byref(session))
    return {'session_id': int(session.value) if ok else None,
            'active_console_session': int(kernel.WTSGetActiveConsoleSessionId())}


def _authentication_id():
    """The LSA logon session this process's token belongs to, as the credential vault sees it."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    security.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                          ctypes.POINTER(ctypes.c_void_p)]
    security.OpenProcessToken.restype = ctypes.c_int
    security.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                             ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    security.GetTokenInformation.restype = ctypes.c_int
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    token = ctypes.c_void_p()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return None
    try:
        stats = TokenStatistics()
        size = ctypes.c_ulong()
        if not security.GetTokenInformation(token, 10, ctypes.byref(stats),
                                           ctypes.sizeof(stats), ctypes.byref(size)):
            return None
        return f'{stats.AuthenticationId >> 32 & 0xFFFFFFFF:08x}-{stats.AuthenticationId & 0xFFFFFFFF:08x}'
    finally:
        kernel.CloseHandle(token)


def facts():
    """Everything that decides which credential view this process reads and writes."""
    import importlib.metadata as metadata

    import keyring.backends.Windows as windows
    from credential_vault import NAMESPACE

    return {
        'pid': os.getpid(),
        'ppid': os.getppid(),
        'executable': sys.executable,
        'python': sys.version.split()[0],
        'prefix': sys.prefix,
        'flags': {'no_user_site': bool(sys.flags.no_user_site), 'ignore_environment': bool(sys.flags.ignore_environment)},
        'session': _session_id(),
        'authentication_id': _authentication_id(),
        'elevated': bool(ctypes.windll.shell32.IsUserAnAdmin()),
        'session_name': os.environ.get('SESSIONNAME'),
        'pythonpath': os.environ.get('PYTHONPATH'),
        'keyring': {'version': metadata.version('keyring'), 'module': windows.__file__,
                    'binding': getattr(windows, 'win32cred', None).__name__},
        'mutex_namespace': NAMESPACE,
        'source_root': str(REPOSITORY / 'src'),
        'probe_root': str(SCRIPT.parent),
    }


# -- child processes -----------------------------------------------------------
def _kind(target, service):
    return 'shared' if target == service else 'compound' if target.endswith('@' + service) else 'other'


def _wait_until(moment):
    if not moment:
        return
    while time.time() < moment:
        time.sleep(0.001)


def child_writer(args):
    """Write this child's own slots through the real wrapper, recording every step."""
    import credential_vault
    from credential_vault import VaultCredentials
    from keyring.backends.Windows import WinVaultKeyring

    service = args.service
    slots = [slot for slot in args.slots.split(',') if slot]
    token = os.environ[TOKEN_VARIABLE]
    records, readbacks = [], []
    state = {'context': 'start'}

    class Observed(WinVaultKeyring):
        """Observation only: the same calls in the same order, plus a record each."""

        def _read_credential(self, target):
            started = time.perf_counter()
            entry = super()._read_credential(target)
            records.append({'op': 'read', 'kind': _kind(target, service),
                            'found': entry is not None,
                            'user': entry['UserName'] if entry else None,
                            'context': state['context'], 'begin': started,
                            'end': time.perf_counter()})
            return entry

        def _set_password(self, target, username, password):
            started = time.perf_counter()
            super()._set_password(target, username, str(password))
            records.append({'op': 'write', 'kind': _kind(target, service), 'user': username,
                            'context': state['context'], 'begin': started,
                            'end': time.perf_counter()})

    real_exclusive = credential_vault.exclusive

    @contextlib.contextmanager
    def observed_exclusive(service_name, timeout_ms=credential_vault.TIMEOUT_MS):
        with real_exclusive(service_name, timeout_ms):
            started = time.perf_counter()
            try:
                yield
            finally:
                records.append({'op': 'hold', 'context': state['context'], 'begin': started,
                                'end': time.perf_counter()})

    credential_vault.exclusive = observed_exclusive
    trace = {'facts': facts(), 'slots': slots, 'records': records, 'readbacks': readbacks}
    try:
        vault = VaultCredentials(Observed())
        trace['mutex'] = credential_vault.mutex_name(service)
        _wait_until(args.start_at)
        for slot in slots:
            state['context'] = 'set:' + slot
            expected = synthetic(token, slot)
            vault.set_password(service, slot, expected)
            state['context'] = 'readback:' + slot
            actual = vault.get_password(service, slot)
            readbacks.append({'slot': slot, 'found': actual is not None,
                              'match': actual == expected})
        trace['error'] = None
    except BaseException as error:  # noqa: BLE001 - the type and code are the evidence
        trace['error'] = {'type': type(error).__name__, 'code': getattr(error, 'code', None)}
        raise
    finally:
        Path(args.trace).write_text(json.dumps(trace), encoding='utf8')


def child_reader(args):
    """Validate every slot of the round from a separate process."""
    from credential_vault import VaultCredentials

    slots = [slot for slot in args.slots.split(',') if slot]
    token = os.environ[TOKEN_VARIABLE]
    vault = VaultCredentials()
    found, matching, seen, absent = 0, 0, set(), []
    for slot in slots:
        actual = vault.get_password(args.service, slot)
        if actual is None:
            absent.append(slot)
            continue
        found += 1
        seen.add(hashlib.sha256(actual.encode('utf8')).hexdigest())
        matching += int(actual == synthetic(token, slot))
    answer = {'facts': facts(), 'expected': len(slots), 'found': found, 'matching': matching,
              'distinct': len(seen), 'absent': absent}
    Path(args.trace).write_text(json.dumps(answer), encoding='utf8')


def child(args):
    if args.child == 'writer':
        child_writer(args)
        return 0
    child_reader(args)
    return 0


# -- analysis ------------------------------------------------------------------
def _overlaps(holds):
    """Pairs of hold intervals from different processes that overlap in real time."""
    ordered = sorted(holds, key=lambda hold: hold['begin'])
    count, details = 0, []
    for index, first in enumerate(ordered):
        for second in ordered[index + 1:]:
            if second['pid'] == first['pid']:
                continue
            if second['begin'] < first['end'] and first['begin'] < second['end']:
                count += 1
                if len(details) < 5:
                    details.append({'a': first['pid'], 'b': second['pid'],
                                    'context_a': first['context'], 'context_b': second['context'],
                                    'overlap_ms': round((min(first['end'], second['end'])
                                                         - max(first['begin'], second['begin'])) * 1000, 3)})
    return count, details


def _replay(events):
    """Which slot each shared write displaced, and whether its read saw that holder."""
    reads = {}
    for event in events:
        if event['op'] == 'read' and event['kind'] == 'shared' and event['context'].startswith('set:'):
            reads[event['context']] = event
    writes = sorted((event for event in events
                     if event['op'] == 'write' and event['kind'] == 'shared'),
                    key=lambda event: event['begin'])
    backups = [event for event in events
               if event['op'] == 'write' and event['kind'] == 'compound']
    problems, stale, previous = [], [], None
    for write in writes:
        read = reads.get(write['context'])
        observed = read['user'] if read and read['found'] else None
        if observed != previous:
            stale.append({'context': write['context'], 'pid': write['pid'],
                          'expected': previous, 'observed': observed,
                          'read_ms': round((read['end'] - read['begin']) * 1000, 3) if read else None})
        if previous is not None and previous != write['user']:
            kept = any(backup['user'] == previous and backup['context'] == write['context']
                       for backup in backups)
            if not kept:
                problems.append({'lost_slot': previous, 'clobbered_by': write['user'],
                                 'pid': write['pid'], 'context': write['context'],
                                 'read_observed': observed})
        previous = write['user']
    return problems, stale


def analyse(traces, reader):
    events = []
    for trace in traces:
        for record in trace.get('records') or []:
            events.append({**record, 'pid': trace['facts']['pid']})
    holds = [event for event in events if event['op'] == 'hold']
    overlaps, overlap_details = _overlaps(holds)
    problems, stale = _replay(events)
    absent = list(reader.get('absent') or [])
    lost = [problem['lost_slot'] for problem in problems]
    return {'lock_holds': len(holds), 'lock_overlaps': overlaps,
            'lock_overlap_details': overlap_details,
            'stale_shared_reads': len(stale), 'stale_details': stale[:5],
            'missing_backups': len(problems), 'problem_details': problems[:5],
            'absent_slots': absent, 'lost_slots': lost,
            'analysis_matches_reader': sorted(lost) == sorted(absent),
            'reader': {key: reader.get(key) for key in
                       ('expected', 'found', 'matching', 'distinct')}}


def every_slot_present(reader):
    expected = WRITERS * SLOTS_PER_WRITER
    return all(type(reader.get(key)) is int and reader[key] == expected
               for key in ('expected', 'found', 'matching', 'distinct'))


def round_passed(summary):
    """Missing evidence is a failed diagnostic, never a successful vault test."""
    analysis = summary['analysis']
    children = summary['children']
    return (summary['reader_returncode'] == 0 and every_slot_present(analysis['reader'])
            and len(children) == WRITERS
            and all(child['returncode'] == 0 and child.get('error') is None
                    and len(child.get('readbacks') or []) == SLOTS_PER_WRITER
                    and all(item.get('found') is True and item.get('match') is True
                            for item in child['readbacks']) for child in children)
            and analysis['lock_holds'] == WRITERS * SLOTS_PER_WRITER * 2
            and all(analysis[key] == 0 for key in
                    ('lock_overlaps', 'stale_shared_reads', 'missing_backups'))
            and analysis['analysis_matches_reader'] is True
            and summary['cleanup']['remaining'] == 0)


# -- one round -----------------------------------------------------------------
def spawn(arguments, token, stderr_path):
    with stderr_path.open('wb') as errors:
        return subprocess.Popen([sys.executable, str(SCRIPT), *arguments],
                                env={**os.environ, TOKEN_VARIABLE: token},
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=errors, cwd=str(SCRIPT.parent),
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def run_round(number, round_dir, service, token):
    round_dir.mkdir(parents=True, exist_ok=False)
    slots = [secrets.token_hex(16) for _ in range(WRITERS * SLOTS_PER_WRITER)]
    start_at = time.time() + START_DELAY
    started = time.perf_counter()
    writers = []
    for index in range(WRITERS):
        mine = slots[index * SLOTS_PER_WRITER:(index + 1) * SLOTS_PER_WRITER]
        writers.append((index, spawn(['--child', 'writer', '--service', service,
                                      '--slots', ','.join(mine), '--start-at', repr(start_at),
                                      '--trace', str(round_dir / f'child-{index}-trace.json')],
                                     token, round_dir / f'child-{index}-stderr.txt')))
    exits = []
    for index, process in writers:
        code = process.wait(timeout=TIMEOUT)
        exits.append({'child': index, 'returncode': code,
                      'seconds': round(time.perf_counter() - started, 3)})
    (round_dir / 'child-exits.json').write_text(json.dumps(exits), encoding='utf8')
    traces = []
    for index, _ in writers:
        path = round_dir / f'child-{index}-trace.json'
        try:
            traces.append(json.loads(path.read_text(encoding='utf8')))
        except (OSError, ValueError):
            traces.append({'facts': {'pid': None}, 'record_error': 'trace missing'})
    reader_path = round_dir / 'reader.json'
    reader_code = spawn(['--child', 'reader', '--service', service,
                         '--slots', ','.join(slots), '--trace', str(reader_path)],
                        token, round_dir / 'reader-stderr.txt').wait(timeout=TIMEOUT)
    try:
        reader = json.loads(reader_path.read_text(encoding='utf8'))
    except (OSError, ValueError):
        reader = {'absent': [], 'record_error': 'reader trace missing'}
    analysis = analyse(traces, reader)
    summary = {'round': number, 'service': service, 'slots': slots,
               'children': [{'returncode': exit_['returncode'], 'error': trace.get('error'),
                             'slots': trace.get('slots'), 'mutex': trace.get('mutex'),
                             'readbacks': trace.get('readbacks'),
                             'facts': trace.get('facts')}
                            for exit_, trace in zip(exits, traces)],
               'reader_returncode': reader_code,
               'reader_facts': reader.get('facts'),
               'analysis': analysis}
    summary['cleanup'] = clean_slots(service, slots)
    summary['passed'] = round_passed(summary)
    (round_dir / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf8')
    return summary


def clean_slots(service, slots):
    """Delete only this round's registered slots, then confirm none of them is readable."""
    from credential_vault import VaultCredentials

    vault = VaultCredentials()
    deleted = 0
    for slot in slots:
        try:
            if vault.get_password(service, slot) is not None:
                vault.delete_password(service, slot)
                deleted += 1
        except Exception:  # noqa: BLE001 - a slot that never arrived has nothing to delete
            continue
    remaining = sum(1 for slot in slots if vault.get_password(service, slot) is not None)
    return {'slots': len(slots), 'deleted': deleted, 'remaining': remaining}


# -- parent --------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', choices=('writer', 'reader'))
    parser.add_argument('--service')
    parser.add_argument('--slots')
    parser.add_argument('--start-at', type=float, default=0.0)
    parser.add_argument('--trace')
    parser.add_argument('--rounds', type=int, default=MAX_ROUNDS)
    args = parser.parse_args()
    if args.child:
        return child(args)
    return run_parent(args)


def run_parent(args):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    # One directory per invocation, so an earlier run's evidence is never overwritten.
    run_dir = EVIDENCE / ('run-' + time.strftime('%Y%m%d-%H%M%S', time.localtime()))
    run_dir.mkdir(parents=True, exist_ok=False)
    token = secrets.token_hex(16)
    report = {'format': 1, 'tool': 'check_vault_visibility', 'writers': WRITERS,
              'slots_per_writer': SLOTS_PER_WRITER, 'max_rounds': args.rounds,
              'evidence': str(run_dir), 'parent': facts(), 'rounds': [],
              'reproduced': False, 'verification_failed': False, 'passed_rounds': 0}
    lines = []

    def say(text):
        lines.append(text)
        print(text, flush=True)

    say(f"evidence={run_dir}")
    say(f"parent pid={report['parent']['pid']} session={report['parent']['session']} "
        f"auth={report['parent']['authentication_id']} elevated={report['parent']['elevated']}")
    say(f"keyring={report['parent']['keyring']['version']} binding={report['parent']['keyring']['binding']}")
    for number in range(1, args.rounds + 1):
        service = SERVICE_PREFIX + secrets.token_hex(8)
        round_dir = run_dir / f'round-{number:02d}'
        summary = run_round(number, round_dir, service, token)
        report['rounds'].append({'round': number, 'service': service,
                                 'directory': str(round_dir), 'passed': summary['passed'],
                                 'analysis': summary['analysis'],
                                 'cleanup': summary['cleanup']})
        for child_summary in summary['children']:
            report['rounds'][-1].setdefault('children', []).append(
                {'returncode': child_summary['returncode'], 'error': child_summary['error'],
                 'readbacks': child_summary['readbacks']})
        analysis = summary['analysis']
        say(f"round {number:02d} passed={summary['passed']} found={analysis['reader']['found']}"
            f"/{analysis['reader']['expected']} matching={analysis['reader']['matching']} "
            f"lock_overlaps={analysis['lock_overlaps']} stale_reads={analysis['stale_shared_reads']} "
            f"missing_backups={analysis['missing_backups']} "
            f"cleanup_remaining={summary['cleanup']['remaining']}")
        if summary['passed']:
            report['passed_rounds'] += 1
            continue
        report['verification_failed'] = True
        result = analysis['reader']
        report['reproduced'] = (result.get('expected') == WRITERS * SLOTS_PER_WRITER
                               and type(result.get('matching')) is int
                               and result['matching'] < result['expected'])
        report['first_failure_round'] = number
        say(f"verification failed in round {number:02d}: reproduced={report['reproduced']} "
            f"absent={analysis['absent_slots']} "
            f"lost={analysis['lost_slots']} analysis_matches_reader={analysis['analysis_matches_reader']}")
        break
    report['conclusion'] = ('reproduced' if report['reproduced']
                            else 'verification incomplete or inconsistent' if report['verification_failed']
                            else 'not reproduced in the rounds that ran')
    (run_dir / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    (run_dir / 'environment.json').write_text(
        json.dumps(report['parent'], indent=2), encoding='utf8')
    (run_dir / 'transcript.txt').write_text('\n'.join(lines) + '\n', encoding='utf8')
    print(json.dumps({'reproduced': report['reproduced'],
                      'conclusion': report['conclusion'],
                      'rounds_run': len(report['rounds']),
                      'passed_rounds': report['passed_rounds'],
                      'evidence': str(run_dir)}, indent=2))
    return 1 if report['verification_failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
