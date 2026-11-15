"""One cross-process critical section around this application's local credential vault.

Why this exists
---------------

Windows Credential Manager holds exactly one generic credential per target name, so the keyring
Windows backend this application uses keeps one *shared* target per service name and emulates
several usernames per service by hand: ``set_password`` reads the credential that currently sits
on the shared target, copies it to the compound target ``<username>@<service>`` when it belongs to
another username, and only then overwrites the shared target; ``get_password`` prefers the shared
target and falls back to the compound one. Those three steps are not one atomic step, so two
processes that prepare their own installation slot at the same moment can both read the same
previous entry and then overwrite the shared target in turn: the credential written first is then
on neither target, and its own read-back verification immediately after the write fails with
``storage_vault_unavailable`` while the database and the store do not exist yet. A later start of
that installation then reports ``storage_vault_missing``. ``check_vault_concurrency.py``
reproduces this with a controllable fake vault and with real Windows processes.

What it does
------------

:class:`VaultCredentials` is a drop-in for ``WinVaultKeyring`` (same ``get_password``,
``set_password``, ``delete_password`` and ``get_credential`` signatures) that holds one named
Windows mutex per service name for the whole duration of every call. One process therefore
finishes its read-backup-overwrite sequence before another one reads, so no update is lost, and
``delete_password``'s read-then-delete can no longer remove the entry another process has just
written. Nothing else changes: the target names, the stored layout, the values, the callers' own
verification and the refusal codes stay exactly as they are, so slots written before this module
existed are read and written as before. It is a different primitive from an in-process thread
lock and from a retry loop: the exclusion covers separate processes.

Crash safety
------------

The mutex is a kernel object, not a file. If the process that owns it exits, crashes or is
killed, the operating system releases it and the next waiter proceeds (``WAIT_ABANDONED``), so an
interrupted installation cannot leave the vault locked. Because the backend backs an entry up
before it overwrites the shared target, an interruption can leave a credential duplicated but
never lost. A waiter that cannot enter within ``TIMEOUT_MS`` fails with the stable
``storage_vault_unavailable`` code and a safe message instead of blocking a start forever; the
caller can retry.

Scope
-----

The mutex name is the hash of the service string inside the session's namespace, so every process
of this user that touches the same service in the same Windows session shares it. Credentials of
other users live in their own vault and other services get their own mutex. Two Windows sessions
of the same user are not covered by a session-local object; that case remains untested and is not needed for this desktop application.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager

from companion_core.errors import AssistantError

VAULT = 'qiban-desktop-probe-storage'
TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 600_000
NAMESPACE = 'Local\\qiban-credential-vault-'
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
BUSY = '本机凭据库正被其他进程使用，请稍后重试。'
UNAVAILABLE = '本机凭据库不可用，请稍后重试。'
_API = None


def _api():
    """The kernel calls this module needs, bound once and only on Windows."""
    global _API
    if _API is None:
        try:
            import ctypes
            from ctypes import wintypes as w

            kernel = ctypes.WinDLL('kernel32', use_last_error=True)

            def bound(name, result, *arguments):
                function = getattr(kernel, name)
                function.restype, function.argtypes = result, arguments
                return function

            _API = {'create': bound('CreateMutexW', w.HANDLE, ctypes.c_void_p, w.BOOL, w.LPCWSTR),
                    'wait': bound('WaitForSingleObject', w.DWORD, w.HANDLE, w.DWORD),
                    'release': bound('ReleaseMutex', w.BOOL, w.HANDLE),
                    'close': bound('CloseHandle', w.BOOL, w.HANDLE)}
        except (ImportError, OSError, AttributeError):
            raise AssistantError('storage_vault_unavailable', UNAVAILABLE) from None
    return _API


def mutex_name(service):
    """The mutex of one service name; a hash keeps the name valid whatever the service is."""
    if not isinstance(service, str) or not service:
        raise AssistantError('storage_vault_unavailable', UNAVAILABLE)
    return NAMESPACE + hashlib.sha256(service.encode('utf8')).hexdigest()[:40]


@contextmanager
def exclusive(service, timeout_ms=TIMEOUT_MS):
    """Hold this service's mutex; the system releases it if the owner dies.

    A thread that already holds it enters again without waiting, so a caller may hold it for a
    longer compound operation and still use the methods below inside.
    """
    if type(timeout_ms) is not int or not 0 < timeout_ms <= MAX_TIMEOUT_MS:
        raise AssistantError('storage_vault_unavailable', UNAVAILABLE)
    api = _api()
    handle = api['create'](None, False, mutex_name(service))
    if not handle:
        raise AssistantError('storage_vault_unavailable', UNAVAILABLE)
    try:
        if api['wait'](handle, timeout_ms) not in (WAIT_OBJECT_0, WAIT_ABANDONED):
            raise AssistantError('storage_vault_unavailable', BUSY)
        try:
            yield
        finally:
            api['release'](handle)
    finally:
        api['close'](handle)


class VaultCredentials:
    """``WinVaultKeyring`` whose every call is one cross-process critical section."""

    def __init__(self, backend=None, *, timeout_ms=TIMEOUT_MS):
        self._backend, self.timeout_ms = backend, timeout_ms

    def backend(self):
        """The Windows keyring backend, created on first use so no import cost is paid early."""
        if self._backend is None:
            from keyring.backends.Windows import WinVaultKeyring

            self._backend = WinVaultKeyring()
        return self._backend

    def hold(self, service=VAULT):
        """Hold the mutex across several calls; nested calls on the same thread stay free."""
        return exclusive(service, self.timeout_ms)

    def get_password(self, service, username):
        with exclusive(service, self.timeout_ms):
            return self.backend().get_password(service, username)

    def set_password(self, service, username, password):
        with exclusive(service, self.timeout_ms):
            return self.backend().set_password(service, username, password)

    def delete_password(self, service, username):
        with exclusive(service, self.timeout_ms):
            return self.backend().delete_password(service, username)

    def get_credential(self, service, username=None):
        with exclusive(service, self.timeout_ms):
            return self.backend().get_credential(service, username)
