"""Time this machine's minimal desktop host from launch to its own clean exit, once and twice again.

One first initialization and two reopens of the same profile, all inside one new evidence
directory. The real host is started the way ``run_managed_launch.py`` starts it: inside a
``ProcessScope`` Job, suspended until the scope owns it, with its profile under the evidence
directory and with the given executable's own assembled resources. It is started with the
existing ``--recovery-check-smoke`` mode, which becomes ready and then exits by itself, so this
script never stops a successful round; it only measures, and keeps every raw log.

Each round reports the existing events ``app_paths``, ``webview_ready``, the application's own
``component_state`` ready (in managed mode the group's id is ``application``) and
``shutdown_completed`` with their relative times, plus the outer wall-clock duration. Round 1
creates the database and the installation; rounds 2 and 3 reopen that same data, so the two kinds
are reported separately. ``events.jsonl`` is appended across rounds, so every round is read from
the byte offset it had before that round's launch.

This is one machine's measurement, not a release gate: no threshold is applied, and nothing here
says anything about long-running stability. Failures keep their evidence and exit non-zero.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

LOCAL = Path(__file__).resolve().parent
PROJECT = LOCAL.parents[1]
# The probe modules sit next to this file and companion_core below src, so both stay importable
# even when the interpreter was started without this checkout on its path.
sys.path.insert(0, str(PROJECT / 'src'))
sys.path.insert(1, str(LOCAL))

from credential_vault import VAULT, VaultCredentials  # noqa: E402

from companion_core.process_scope import Accounting, ExtendedLimits, ProcessScope  # noqa: E402

EXE_ARGUMENT = '--recovery-check-smoke'  # the existing mode that exits by itself once ready
ROUNDS = ('first_initialization', 'reopen_same_profile', 'reopen_same_profile')
MILESTONES = ('app_paths', 'webview_ready', 'application_ready', 'shutdown_completed')
ROUND_DEADLINE_SECONDS = 900
NATURAL_EXIT_WAIT_SECONDS = 10
JOB_BASIC_ACCOUNTING_INFORMATION = 1
JOB_EXTENDED_LIMIT_INFORMATION = 9
IDENTITY = re.compile(r'[0-9a-f]{32}')

MEMORY_MEASUREMENT = {
    'source': 'Windows 作业对象 JOBOBJECT_EXTENDED_LIMIT_INFORMATION 的 PeakJobMemoryUsed（无新增依赖）',
    'meaning': '本轮这份 Job 内全部成员进程在同一时刻占用已提交内存之和的峰值，由内核累计；'
               '不是工作集（RSS），也不是各进程峰值相加',
    'scope': '本脚本创建并持有的 Job，含 Qiban.exe 以及留在该 Job 内的后代'
             '（安装、存储、PostgreSQL、WebView2）',
    'caveats': ['在 scope.close() 之前查询', '以 CREATE_BREAKAWAY_FROM_JOB 跳出该 Job 的进程不计入',
                '为零时只记 available=false，不据此判定通过或失败'],
}

BOUNDARIES = {
    'workload': '本机最小桌面＋存储＋PostgreSQL；真实桌面宿主，不接账号、不连平台、不调模型',
    'rounds': '固定 1 次首次初始化（含建库）＋ 2 次同一资料的普通重开',
    'timing': '相对毫秒数取自应用既有事件自带的 milliseconds（窗口对象构造后开始计时）；'
              'outer_total_ms 是本脚本从创建进程到该进程自行退出的本机实测耗时',
    'not_measured': ['发布阈值（本脚本不设，也不判通过率）',
                     '72 小时或任意长时稳定性（本轮不构成结论）',
                     '物理断电、干净机器与安装包验收'],
}


def sha256_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def optional_json(path):
    try:
        return json.loads(path.read_text(encoding='utf8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def round_log_bytes(log, offset):
    """This round's own bytes: the log is appended across rounds, so the pre-launch size cuts it."""
    return log.read_bytes()[offset:] if log.exists() else b''


def parse_events(raw):
    """Only complete lines; a write still in progress belongs to no round yet."""
    complete = raw[:raw.rfind(b'\n') + 1]
    events = []
    for line in complete.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def first_event(events, name, wanted=None):
    for event in events:
        if event.get('name') == name and (wanted is None or wanted(event)):
            return event
    return None


def application_ready(event):
    detail = event.get('detail') or {}
    return detail.get('id') == 'application' and detail.get('state') == 'ready'


def ready_event(events):
    return first_event(events, 'component_state', application_ready)


def moment(event):
    return None if event is None else event.get('milliseconds')


def spans(times):
    return {f'{before}_to_{after}': (None if times[before] is None or times[after] is None
                                     else times[after] - times[before])
            for before, after in zip(MILESTONES, MILESTONES[1:])}


def job_peak_commit(scope):
    """Peak committed memory the kernel counted for this round's whole Job, queried before close."""
    limits = ExtendedLimits()
    if not scope.api.QueryInformationJobObject(scope.handle, JOB_EXTENDED_LIMIT_INFORMATION,
                                               ctypes.byref(limits), ctypes.sizeof(limits), None):
        return {'available': False}
    peak_job = int(limits.peak_job_memory)
    return {'available': peak_job > 0, 'peak_job_commit_bytes': peak_job,
            'peak_process_commit_bytes': int(limits.peak_process_memory)}


def active_members(scope):
    """How many processes this round's Job still accounts for, through the scope's own accounting."""
    accounting = Accounting()
    if not scope.api.QueryInformationJobObject(scope.handle, JOB_BASIC_ACCOUNTING_INFORMATION,
                                               ctypes.byref(accounting), ctypes.sizeof(accounting), None):
        return None
    return int(accounting.active)


def natural_exit(scope):
    """Watch the Job drain by itself; scope.close() must not be what makes the members go away."""
    started = time.monotonic()
    deadline = started + NATURAL_EXIT_WAIT_SECONDS
    active = active_members(scope)
    while active not in (None, 0) and time.monotonic() < deadline:
        time.sleep(.05)
        active = active_members(scope)
    return {'waited_ms': round((time.monotonic() - started) * 1000), 'active_members': active,
            'natural': active == 0}


def installation_identity(profile):
    """The identity this evidence's own new profile installed, for a round that never reported one."""
    state = optional_json(profile / 'data' / 'installation.json') or {}
    value = state.get('installation')
    return value if isinstance(value, str) and IDENTITY.fullmatch(value) else None


def clean_test_slot(vault, identity):
    """Delete only the credential slot this evidence created, as ``run_managed_launch.py`` does."""
    if not identity:
        return {'slots': 0, 'deleted': 0, 'remaining': None, 'identity_known': False}
    present = False
    try:
        present = vault.get_password(VAULT, identity) is not None
        if present:
            vault.delete_password(VAULT, identity)
        remaining = int(vault.get_password(VAULT, identity) is not None)
    except Exception as error:  # noqa: BLE001 - an unavailable vault is reported, never hidden
        return {'slots': 1, 'deleted': 0, 'remaining': None, 'identity_known': True,
                'error': type(error).__name__ + ': ' + str(error)}
    return {'slots': 1, 'deleted': int(present), 'remaining': remaining, 'identity_known': True}


def run_round(exe, profile, evidence, environment, number, kind, identity):
    log = profile / 'logs' / 'events.jsonl'
    offset = log.stat().st_size if log.exists() else 0
    installation = profile / 'data' / 'installation'
    record = {'round': number, 'kind': kind, 'log_offset_bytes': offset, 'memory': None,
              'milestones_ms': {}, 'spans_ms': {}, 'checks': {}, 'passed': False}
    scope = ProcessScope()
    process = None
    started = time.monotonic()
    try:
        process = subprocess.Popen([str(exe), EXE_ARGUMENT, '--data-dir', str(profile)], cwd=evidence,
                                   env=environment, stdin=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
        scope.attach(process)
        scope.resume()
        record['exit_code'] = process.wait(timeout=ROUND_DEADLINE_SECONDS)
    except subprocess.TimeoutExpired:
        record['failure'] = f'the host did not exit within {ROUND_DEADLINE_SECONDS} s'
    except Exception as error:  # noqa: BLE001 - this round keeps its record, logs and failure
        record['failure'] = type(error).__name__ + ': ' + str(error)
    finally:
        record['outer_total_ms'] = round((time.monotonic() - started) * 1000)  # up to the host's exit
        try:
            # Only a host that exited 0 may claim a natural drain; the forced cleanup below runs in
            # every case and its own success says nothing about how the tree ended.
            record['natural_exit'] = (natural_exit(scope) if record.get('exit_code') == 0 else
                                      {'natural': False, 'active_members': None,
                                       'reason': 'the host did not exit with code 0'})
        finally:
            try:
                if process is not None and not scope.attached and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                record['memory'] = job_peak_commit(scope)
            finally:
                try:
                    scope.close()
                    record['owned_scope_closed'] = True
                except Exception as error:  # noqa: BLE001 - the cleanup outcome is recorded, not assumed
                    record['owned_scope_closed'] = False
                    record['scope_error'] = type(error).__name__ + ': ' + str(error)
        raw = round_log_bytes(log, offset)
        (evidence / f'round-{number}-events.jsonl').write_bytes(raw)
        events = parse_events(raw)
    record['log_slice_bytes'] = len(raw)
    record['log_event_names'] = [event.get('name') for event in events]

    found = {'app_paths': first_event(events, 'app_paths'),
             'webview_ready': first_event(events, 'webview_ready'),
             'application_ready': ready_event(events),
             'shutdown_completed': first_event(events, 'shutdown_completed')}
    times = {name: moment(event) for name, event in found.items()}
    record['milestones_ms'] = times
    record['spans_ms'] = spans(times)
    confirmed = first_event(events, 'check_recovery_only_ready_pass')
    if confirmed is not None:
        record['recovery_only_ready_confirmed_ms'] = moment(confirmed)
    shutdown = found['shutdown_completed']
    state = optional_json(installation / 'installation.json') or {}
    session = optional_json(profile / 'data' / 'application-session.json') or {}
    record['installation'] = {'identity': state.get('installation'), 'phase': state.get('phase')}
    record['application_session_stage'] = session.get('Stage')

    checks = record['checks']
    checks['app_paths_event'] = found['app_paths'] is not None
    checks['webview_ready_event'] = found['webview_ready'] is not None
    checks['application_ready_event'] = found['application_ready'] is not None
    checks['one_round_in_log'] = sum(1 for event in events if event.get('name') == 'app_paths') == 1
    ordered = [times[name] for name in MILESTONES]
    checks['milestones_in_order'] = None not in ordered and ordered == sorted(ordered)
    checks['shutdown_completed_graceful'] = (shutdown or {}).get('detail') == {
        'state': 'stopped', 'detail': 'graceful_stop'}
    checks['exit_code_zero'] = record.get('exit_code') == 0
    checks['application_session_clean'] = session.get('Stage') == 'clean'
    checks['installation_phase_ready'] = state.get('phase') == 'ready'
    checks['identity_unchanged'] = bool(state.get('installation')) and (
        identity is None or state.get('installation') == identity)
    checks['no_test_database_postmaster_pid'] = not (installation / 'database' / 'postmaster.pid').exists()
    checks['identity_format'] = IDENTITY.fullmatch(str(state.get('installation') or '')) is not None
    checks['owned_tree_exited_naturally'] = (record.get('natural_exit') or {}).get('natural') is True
    record['passed'] = all(checks.values())
    return record


def summarize(rounds):
    groups = {}
    for record in rounds:
        group = groups.setdefault(record['kind'],
                                  {'rounds': [], 'outer_total_ms': [],
                                   'milestones_ms': {name: [] for name in MILESTONES}})
        group['rounds'].append(record['round'])
        group['outer_total_ms'].append(record['outer_total_ms'])
        for name, value in record['milestones_ms'].items():
            group['milestones_ms'][name].append(value)
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True,
                        help='已有构建的 Qiban.exe（本机 ui-178-app 打包产物）')
    parser.add_argument('--evidence', type=Path, required=True,
                        help='必须尚不存在的目录，且位于 .dev/refactor-r0/desktop 之下；资料只写在这里')
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('this baseline measures the Windows desktop host')
    if not args.exe.is_file():
        raise SystemExit(f'--exe is not a file: {args.exe}')
    exe = args.exe.resolve(strict=True)
    evidence = args.evidence.resolve()
    allowed = (PROJECT / '.dev' / 'refactor-r0' / 'desktop').resolve()
    if evidence == allowed or not evidence.is_relative_to(allowed):
        raise SystemExit(f'--evidence must be a new directory under {allowed}')
    if evidence.exists():
        raise SystemExit('--evidence must not exist yet; each run keeps its own evidence')
    evidence.mkdir(parents=True)

    profile = evidence / '中文用户'
    environment = {**os.environ, 'PATH': str(Path(os.environ['SystemRoot']) / 'System32'),
                   'PYTHONHOME': str(evidence / 'no-python')}
    report = {'passed': False, 'script': 'scripts/desktop_probe/run_startup_baseline.py',
              'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'exe': {'path': str(exe), 'sha256': sha256_of(exe),
                      'size_bytes': exe.stat().st_size},
              'host': {'os': platform.platform(), 'cpu_count': os.cpu_count()},
              'evidence': str(evidence), 'profile': str(profile), 'host_mode': EXE_ARGUMENT,
              'memory_measurement': MEMORY_MEASUREMENT, 'boundaries': BOUNDARIES, 'rounds': []}
    vault = VaultCredentials()
    identity, failure = None, None
    try:
        for number, kind in enumerate(ROUNDS, 1):
            record = run_round(exe, profile, evidence, environment, number, kind, identity)
            report['rounds'].append(record)
            identity = identity or record['installation']['identity']
            if not record['passed']:
                failed = sorted(name for name, ok in record['checks'].items() if not ok)
                failure = f"round {number} ({kind}) failed: {', '.join(failed)}"
                if record.get('failure'):
                    failure += '; ' + record['failure']
                break
    except Exception as error:  # noqa: BLE001 - the report below is the evidence of the failure
        failure = f'{type(error).__name__}: {error}'
    finally:
        # A round can fail while it is reading its own logs, before the loop has an identity; this
        # evidence's own new profile still holds it, and only a 32-hex one of it may be deleted.
        identity = identity or installation_identity(profile)
        report['identity'] = identity
        report['cleanup'] = clean_test_slot(vault, identity)
        report['groups'] = summarize(report['rounds'])
        report['raw_logs_retained'] = str(profile / 'logs' / 'events.jsonl')
        report['release_threshold'] = None
        report['long_run_claim'] = False
        report['failure'] = failure
        report['passed'] = (failure is None and all(record['passed'] for record in report['rounds'])
                           and len(report['rounds']) == len(ROUNDS)
                           and report['cleanup']['remaining'] == 0)
        report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        (evidence / 'startup-baseline.json').write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding='utf8')
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
