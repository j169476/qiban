"""The native host's private stdio bridge for one maintenance step of an upgrade transaction.

Nothing outside this machine's own maintenance window can reach this module. The window owns a Job
(``OwnedProcess``) and starts its own bridge, ``--upgrade-host-bridge``; that bridge accepts only its
parent on a current-user-only pipe, receives the Windows-verification result there and starts this
bundled backend (``--upgrade-host``) with it on the child's own stdin. So the verification crosses
exactly one window -> an owned subprocess -> a backend's stdin, and is installed as the
``upgrade.verification`` channel before any verb runs. A file, a command-line flag or an environment
variable can never stand in for it, and an ordinary invocation from a shell is refused because its
parent process is the maintenance window's bridge, which runs from this transaction's own carrier
copy. Because the bridge lives in the window's Job, cancelling or killing the window kills the whole
tree - this process included - without any watchdog of its own.

One invocation handles exactly one verb, so each half of an ordinary rollback runs in the program
that is allowed to run it: ``rollback-prepare`` in the newly installed program (the only code that
can read the new schema) and ``rollback`` in the carrier's own program (the previous version, whose
code and PostgreSQL binaries are exactly the ones this machine reads afterwards). ``emergency`` is
the D62 path: the new program's maintenance backend cannot run, so the carrier seals the fault data
as raw material and returns to the upgrade's original selection - upgraded content does not appear
and the local settings are the older generation's own.

Wiring, owned by the bundled backend (``storage_worker.py``; not part of this module):

    parser.add_argument("--upgrade-host", nargs=3, metavar=("ROOT", "APP", "VERB"))
    ...
    if args.upgrade_host:
        import upgrade_host
        return upgrade_host.main(*args.upgrade_host)

``VERBS`` names the three accepted verbs for that entry's own ``choices``. There is no other entry:
no page route, management operation, model tool or ordinary command line reaches a rollback.

The reply carries fixed fields only: a verdict, a code, a phase and the display name of one
preserved or sealed copy. No key, password, path or backend text ever crosses back.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from upgrade import journal, program, verification

from companion_core.errors import AssistantError

VERBS = ('rollback-prepare', 'rollback', 'emergency')
RECEIPT_FIELDS = frozenset({'session', 'at'})
MAX_RECEIPT = 4096
QUERY_LIMITED_INFORMATION = 0x1000
CODE = re.compile('[a-z0-9_]{1,64}')
PHASE = re.compile('[a-z-]{1,32}')
DISPLAY = re.compile(r'[^\x00-\x1f/\\:]{1,200}')
HOST = ('upgrade_host_parent', '维护请求不是由栖伴自己的维护窗口发出的，未执行任何操作。')
RECEIPT = ('upgrade_host_receipt', '维护窗口没有提供有效的 Windows 身份验证结果，未执行任何操作。')
FAILED = ('upgrade_host_failed', '维护操作没有完成，请重新打开维护窗口检查升级状态。')
ROOT = ('upgrade_root', '本机资料目录不可用，未继续维护操作。')
PROGRAM = ('upgrade_program_missing', '程序目录不可用，未继续维护操作。')


def _refuse(pair):
    raise AssistantError(*pair)


def parent_image():
    """Image path of the process that started this one; None when Windows will not say."""
    parent = os.getppid()
    if parent <= 0:
        return None
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        open_process = kernel.OpenProcess
        open_process.restype, open_process.argtypes = wintypes.HANDLE, (
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        close_handle = kernel.CloseHandle
        close_handle.restype, close_handle.argtypes = wintypes.BOOL, (wintypes.HANDLE,)
        query = kernel.QueryFullProcessImageNameW
        query.restype, query.argtypes = wintypes.BOOL, (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        handle = open_process(QUERY_LIMITED_INFORMATION, False, parent)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not query(handle, 0, buffer, ctypes.byref(size)):
                return None
            return buffer.value or None
        finally:
            close_handle(handle)
    except (AttributeError, OSError):
        return None


def carrier_tree(root, record):
    """The transaction's carrier tree, taken from this machine's record and never from a caller."""
    return program.tree(root, record['carrier']['identity_sha256'])


def require_host(root):
    """Refuse unless this process was started by the carrier's own maintenance window.

    The parent is the window's bridge, whose own image is the carrier's desktop program, so checking
    that path is the existing parent check and nothing more is needed: the receipt itself only ever
    travels from the window onto this process's stdin. The returned record is the one this
    invocation works on, so every verb sees the same state.
    """
    record = journal.load(root, required=True)
    try:
        tree = carrier_tree(root, record).resolve()
        image = parent_image()
        parent = Path(image).resolve() if image is not None else None
        trusted = parent is not None and os.path.normcase(str(parent)).startswith(
            os.path.normcase(str(tree)+os.sep))
    except (OSError, RuntimeError, KeyError):
        trusted = False
    if not trusted:
        _refuse(HOST)
    return record


def read_receipt(stream):
    """Read the one verification result the host wrote; nothing else is ever accepted here."""
    try:
        raw = stream.readline(MAX_RECEIPT+1)
    except (OSError, ValueError):
        _refuse(RECEIPT)
    if not raw or len(raw) > MAX_RECEIPT:
        _refuse(RECEIPT)
    try:
        value = json.loads(raw.decode('utf8'))
    except (ValueError, UnicodeError):
        _refuse(RECEIPT)
    if not isinstance(value, dict) or set(value) != RECEIPT_FIELDS:
        _refuse(RECEIPT)
    session, at = value['session'], value['at']
    if not isinstance(session, str) or not 0 < len(session) <= 200:
        _refuse(RECEIPT)
    if not isinstance(at, str) or not 0 < len(at) <= 64:
        _refuse(RECEIPT)
    try:
        taken = datetime.fromisoformat(at)
    except ValueError:
        _refuse(RECEIPT)
    if taken.tzinfo is None:
        _refuse(RECEIPT)
    return {'session': session, 'at': at}


def install_receipt(receipt):
    """Install the host's one receipt as this process's verification channel."""

    def handler(request):
        # The channel is asked once, for the rollback it authorised, and for nothing else.
        if not isinstance(request, dict) or request.get('action') != verification.ACTION:
            _refuse(verification.INVALID)
        return {'session': receipt['session'], 'at': receipt['at']}

    verification.install(handler)


def perform(root, installed, verb, record):
    """Run the one verb this invocation was started for, in the program allowed to run it."""
    from upgrade import service

    if verb == 'emergency':
        return emergency_step(root, installed, service, record)
    if verb == 'rollback-prepare':
        return service.rollback_prepare(root, app=installed)
    return service.rollback(root, installed_app=installed)


def emergency_step(root, installed, service, record):
    """The D62 way back: seal the faulty installation, then switch back from the carrier.

    Two verbs of the same carrier program, in this order: the backend seals the faulty installation
    offline, then brings the previous build back. Sealing is skipped once the record is past it: an
    interrupted switch is finished from its own recorded plan, and sealing again would be refused by
    the backend for exactly that reason. Both take the program directory from the process itself.
    """
    if record.get('phase') in ('staged', 'confirmed', 'emergency'):
        service.emergency_seal(root)
    return service.emergency_rollback(root, installed_app=installed)


def display(*values):
    """A name usable in one sentence; anything else is dropped instead of being shown."""
    for value in values:
        if isinstance(value, str) and DISPLAY.fullmatch(value):
            return value
        if isinstance(value, dict):
            for key in ('name', 'display', 'file'):
                inner = value.get(key)
                if isinstance(inner, str) and DISPLAY.fullmatch(inner):
                    return inner
    return None


def retained(source):
    """Whether the previous build came back over a directory that kept files nobody published.

    The emergency way back cannot always know the newer build's complete file set, and it never
    deletes what neither document names. That is a fact the person has to hear, as a fixed flag -
    the backend's own note text stays where it is.
    """
    if source.get('program_files') == 'partial':
        return True
    kept = source.get('retained')
    return isinstance(kept, dict) and type(kept.get('count')) is int and kept['count'] > 0


def summary(verb, value):
    """Only fixed fields cross back: a verdict, a code, a phase and one display name."""
    source = value if isinstance(value, dict) else {}
    ok = source.get('ok') is True
    code, phase = source.get('error'), source.get('phase')
    return {'ok': ok, 'verb': verb,
            'error': None if ok else (code if isinstance(code, str) and CODE.fullmatch(code)
                                      else FAILED[0]),
            'phase': phase if isinstance(phase, str) and PHASE.fullmatch(phase) else None,
            'preservation': display(source.get('preservation')),
            'sealed': display(source.get('sealed'), source.get('fault'), source.get('archive')),
            'retained': retained(source)}


def scope(root, installed):
    """The maintenance scope of this invocation: a real root and one real program directory."""
    root, installed = Path(root), Path(installed)
    if not root.is_dir() or not (root/'installation.json').is_file():
        _refuse(ROOT)
    if not installed.is_dir():
        _refuse(PROGRAM)
    return root, installed


def emit(stream, value):
    """One JSON line on the host's own handle; ASCII only, so no console encoding is involved."""
    if stream is None:
        return 1
    try:
        stream.write((json.dumps(value, ensure_ascii=True, sort_keys=True)+'\n').encode('ascii'))
        stream.flush()
    except (OSError, ValueError):
        return 1
    return 0 if value.get('ok') is True else 1


def main(root, installed, verb, *, stdin=None, stdout=None):
    """One host-authorised verb: its receipt first, then the verb, then one bounded summary."""
    source = stdin if stdin is not None else getattr(sys.stdin, 'buffer', None)
    target = stdout if stdout is not None else getattr(sys.stdout, 'buffer', None)
    answer = {'ok': False, 'verb': verb, 'error': FAILED[0], 'phase': None,
              'preservation': None, 'sealed': None, 'retained': False}
    if source is None or verb not in VERBS:
        return emit(target, answer)
    try:
        # The scope and the host's own identity are checked before anything is read, so an
        # ordinary CLI invocation is refused without consuming or trusting any stdin.
        root, installed = scope(root, installed)
        record = require_host(root)
        install_receipt(read_receipt(source))
        answer = summary(verb, perform(root, installed, verb, record))
    except AssistantError as error:
        code = str(error.code)
        answer = {**answer, 'error': code if CODE.fullmatch(code) else FAILED[0]}
    except Exception:  # noqa: BLE001 - a fixed code, never a traceback or backend text
        answer = {**answer, 'error': FAILED[0]}
    return emit(target, answer)
