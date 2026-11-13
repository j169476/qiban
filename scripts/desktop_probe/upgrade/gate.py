"""Who may open this data root while an upgrade transaction is open.

An upgrade transaction spans two things that cannot move together: the published program and
the selected data generation. Between the moment the pointer names a clone and the moment the
new program has validated it, *nobody* may be allowed to open this root for ordinary business -
not the previous program (its writes would be thrown away by a later rollback) and not any
worker started from the old installation (same reason).

The decision therefore depends on both facts, never on a marker alone:

    no record, or a finished one      open as usual
    prepared / staging                nobody: the old data and the clone are being prepared
    staged                            only the recorded target may initialize/validate;
                                      ordinary business remains blocked until confirmation
    rollback-ready                    nobody: the next step switches back
    emergency / emergency-switching   nobody: the faulty installation is being sealed offline and
                                      the previous build is coming back
    maintenance verb                  the transaction's own actor, always

An unknown program identity is never treated as the target: a worker started from a directory
without a published identity record is held out like any other stranger.
"""
from __future__ import annotations

from pathlib import Path

from companion_core.errors import AssistantError

from . import journal, program

BUSINESS = 'business'
INITIALIZE = 'initialize'
MAINTENANCE = 'maintenance'
PURPOSES = frozenset({BUSINESS, INITIALIZE, MAINTENANCE})


def target_program(app):
    """The published identity of the program that owns this executable, or None."""
    directory = Path(app)
    if not (directory/program.IDENTITY).is_file():
        return None
    try:
        published = program.document(directory)
        program.verify(directory, published)
        return published['sha256']
    except (AssistantError, OSError):
        return None


def application_of(executable):
    """`<app>` of a frozen worker: `<app>/resources/storage/<worker>`; None when not frozen."""
    path = Path(executable)
    if path.parent.name != 'storage' or path.parent.parent.name != 'resources':
        return None
    return path.parent.parent.parent


def running_identity(executable):
    """The published identity of the program this worker belongs to, or None when unknown."""
    app = application_of(executable)
    return None if app is None else target_program(app)


def decide(record, program_identity, purpose):
    """One decision; every denial carries the stable code and the sentence a person can act on."""
    if purpose not in PURPOSES:
        raise AssistantError('upgrade_state', '未知的打开用途。')
    if purpose == MAINTENANCE:
        return {'allow': True, 'code': None, 'phase': None, 'message': ''}
    if record is None or record['phase'] in journal.TERMINAL_PHASES:
        return {'allow': True, 'code': None, 'phase': record['phase'] if record else None,
                'message': ''}
    phase = record['phase']
    target = record['target']['identity_sha256']
    if phase in ('prepared', 'staging'):
        return {'allow': False, 'phase': phase, 'code': 'upgrade_maintenance_in_progress',
                'message': '栖伴正在准备一次程序升级，暂时不能打开资料。请完成或放弃这次升级后再启动。'}
    if phase == 'rollback-ready':
        return {'allow': False, 'phase': phase, 'code': 'upgrade_rollback_pending',
                'message': '栖伴正在回退上一次升级，暂时不能打开资料。请完成回退后再启动。'}
    if phase in ('emergency', 'emergency-switching'):
        return {'allow': False, 'phase': phase, 'code': 'upgrade_rollback_pending',
                'message': '栖伴正在把故障版本的本机资料封存并紧急回退，暂时不能打开资料。'
                           '请完成回退后再启动。'}
    if phase == 'staged':
        if isinstance(program_identity, str) and program_identity == target:
            if purpose == INITIALIZE:
                return {'allow': True, 'code': None, 'phase': phase, 'message': ''}
            return {'allow': False, 'phase': phase, 'code': 'upgrade_confirmation_required',
                    'message': '新版本尚未完成升级校验，暂不启动任务和平台。'}
        if program_identity is None:
            return {'allow': False, 'phase': phase, 'code': 'upgrade_identity_unknown',
                    'message': '这次升级还没有被新版本确认，且无法确认当前程序的身份，因此不能打开资料。'
                               '请从这次升级记录的维护载体启动，完成确认或回退。'}
        return {'allow': False, 'phase': phase, 'code': 'upgrade_previous_program_blocked',
                'message': '资料已经交给新版本校验，旧版本不能继续写入，否则回退会丢掉这些写入。'
                           '请从这次升级记录的维护载体完成确认或回退。'}
    return {'allow': False, 'phase': phase, 'code': 'upgrade_state',
            'message': '升级事务记录处于无法识别的状态，未打开资料。'}


def require(root, program_identity, purpose):
    """Raise the recorded refusal, or return the decision that allows this open."""
    record = journal.load(root)
    decision = decide(record, program_identity, purpose)
    if not decision['allow']:
        raise AssistantError(decision['code'], decision['message'])
    if record is not None and record['phase'] == 'staged':
        carrier = program.tree(root, record['carrier']['identity_sha256'])
        published = program.document(carrier)
        mark = program.carrier_record(carrier)
        if (published['sha256'] != record['carrier']['identity_sha256']
                or mark['installation'] != record['installation']):
            raise AssistantError('upgrade_program_changed', '升级维护副本与本机记录不符。')
        program.verify(carrier, published)
    return decision


def identity_for_root(root, executable):
    """Normal opens do not hash 1.3 GB; an unfinished upgrade requires verified bytes."""
    record = journal.load(root)
    if record is None or record['phase'] in journal.TERMINAL_PHASES:
        return None
    return running_identity(executable)
