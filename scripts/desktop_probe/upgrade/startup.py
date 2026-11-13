"""What one program start does with an unfinished upgrade transaction, in a single decision.

A ``staged`` upgrade is the only state in which a start has something to do: the program whose
code is running is the recorded target, so it validates the clone it has been handed and confirms
the pairing. Everything else is one of two things - nothing to do (no record, or a finished one)
or a refusal. While an upgrade is being prepared, handed over or rolled back, ordinary business
may not open this root: a write made now would be thrown away by the step that is still to come,
so those phases return a stable refusal code instead of a partially usable root.

Two rules keep this entry harmless:

* the confirmation is the only effect, and only from ``staged``. No other phase is moved to
  ``confirmed``, and no caller supplies the program directory: the directory of the program whose
  code is running is derived from the process (``service.executing_app``), so a directory passed
  on a command line can never stand in for the program that is actually running.
* a confirmed upgrade is never reported as failed because the follow-up cleanup left work behind.
  Cleanup's own result keeps its reasons - which registered artifact, which code, which skips -
  and becomes a ``warning`` on a successful answer, for the host to show later. It is not dropped
  and does not turn a finished upgrade into a broken one, and the sentence it carries promises
  only that the cleanup can run again, never that something retried it.

The two "nothing to do" answers read the record and nothing else: a normal start must not pay the
published payload's hashing cost before the page is up. A warning read from an already confirmed
record is just as cheap - the record's own cleanup result is mapped, nothing is hashed, the cleanup
is not run again, and nothing claims it was.
"""
from __future__ import annotations

from companion_core.errors import AssistantError

from . import journal, service

NONE = 'none'
CONFIRM = 'confirm'

# The same stable codes the business-open gate uses, so a person sees one vocabulary. A phase this
# table does not know is never guessed at: it is refused as an unknown record state.
REFUSALS = {
    'prepared': ('upgrade_maintenance_in_progress',
                 '栖伴正在准备一次程序升级，暂时不能打开资料。请完成或放弃这次升级后再启动。'),
    'staging': ('upgrade_maintenance_in_progress',
                '栖伴正在准备一次程序升级，暂时不能打开资料。请完成或放弃这次升级后再启动。'),
    'rollback-ready': ('upgrade_rollback_pending',
                       '栖伴正在回退上一次升级，暂时不能打开资料。请完成回退后再启动。'),
    'rolling-back': ('upgrade_rollback_pending',
                     '栖伴正在回退上一次升级，暂时不能打开资料。请完成回退后再启动。'),
    'emergency': ('upgrade_rollback_pending',
                  '栖伴正在把故障版本的本机资料封存并紧急回退，暂时不能打开资料。请完成回退后再启动。'),
    'emergency-switching': ('upgrade_rollback_pending',
                            '栖伴正在紧急回退上一次升级，暂时不能打开资料。请完成回退后再启动。'),
}
UNKNOWN = ('upgrade_state', '升级事务记录处于无法识别的状态，未打开资料。')


def _refuse(code, message):
    raise AssistantError(code, message)


def _entries(value, field):
    """The failed or skipped list of one cleanup result, in either recorded shape.

    A finished run returns its report under ``cleanup``; the record written for a later start
    holds the same lists at its top level. Both are read the same way, and a missing list is an
    empty one - the reasons are carried, never invented.
    """
    report = value.get('cleanup') if isinstance(value.get('cleanup'), dict) else value
    entries = report.get(field)
    return entries if isinstance(entries, list) else []


def warning(cleanup):
    """A confirmed upgrade whose cleanup did not finish: its reasons, kept for later display.

    The structured result comes first: ``cleanup.clean`` reports leftover work as
    ``complete: false`` *and* ``ok: false`` with its ``failed`` entries, so testing the wrapper
    first would throw those reasons away. Only a cleanup that could not run at all - no report,
    an error code and a reason - has no structured result to read.
    """
    if not isinstance(cleanup, dict):
        return None
    if cleanup.get('complete') is False:
        return {'code': 'upgrade_cleanup_incomplete',
                'failed': _entries(cleanup, 'failed'), 'skipped': _entries(cleanup, 'skipped'),
                'message': '升级已确认，旧代次没有全部清理；原因已保留，可以再次清理。'}
    if cleanup.get('ok') is False:
        return {'code': 'upgrade_cleanup_failed', 'error': cleanup.get('error'),
                'reason': cleanup.get('reason'),
                'message': '升级已确认，但清理没有完成；原因已保留，可以再次清理。'}
    return None


def settled(record):
    """The warning a finished upgrade already carries in its own record; nothing is redone."""
    if record['phase'] != 'confirmed':
        return None
    return warning(record['cleanup'])


def confirm(root, *, vault=None):
    """Decide one start: confirm a staged upgrade by the running program, or refuse to open.

    ``ready`` is the only answer that lets the host go on to start ordinary business.
    """
    record = journal.load(root)
    if record is None:
        return {'ready': True, 'action': NONE, 'phase': None, 'warning': None}
    phase = record['phase']
    if phase in journal.TERMINAL_PHASES:
        return {'ready': True, 'action': NONE, 'phase': phase, 'warning': settled(record)}
    if phase == 'staged':
        result = service.confirm(root, service.executing_app(), vault=vault)
        return {'ready': True, 'action': CONFIRM, 'phase': result['phase'],
                'transaction': result.get('transaction'), 'warning': warning(result.get('cleanup'))}
    code, message = REFUSALS.get(phase, UNKNOWN)
    _refuse(code, message)
