"""One maintenance critical section shared by every verb of an upgrade transaction.

The same two files guard preparation, confirmation, cleanup and rollback, so no two of them can
overlap, and the installation lock is the file a first install or an upgrade start holds for its
whole run - a maintenance step therefore cannot run beside component preparation either. Cleanup
additionally takes the portable restore lock, so a restore cannot start in the middle of a
cleanup that is dropping databases; the preparation path must *not* take it, because the clone
itself is performed by ``portable_restore`` which takes that same lock.

The lock files are bookkeeping only; nothing here reads or writes data. Never nest two calls in
one thread: two ``FileLock`` objects on the same file would wait on each other, so a verb that
needs the section holds it once and calls helpers that accept the section as already held.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

from filelock import FileLock, Timeout

from companion_core.errors import AssistantError

from . import journal

# The same file name portable_restore uses for a staging switch (``portable_restore.LOCK_FILE``).
RESTORE = 'restore.lock'
BUSY = 'upgrade_busy'
LOCK_FAILED = 'upgrade_lock'


@contextlib.contextmanager
def exclusive(root, *, restore=False):
    """Hold this installation's maintenance section; never nested in the same thread."""
    journal.directory(root, create=True)
    names = [journal.DIRECTORY+'/'+journal.LOCK, 'installation.lock']
    if restore:
        names.append(RESTORE)
    locks = []
    try:
        for name in names:
            lock = FileLock(str(Path(root)/name), timeout=0)
            try:
                lock.acquire()
            except Timeout:
                raise AssistantError(BUSY, '另一个升级维护操作正在进行，请稍后重试。') from None
            except OSError:
                raise AssistantError(LOCK_FAILED,
                                     '升级维护锁不可用，请检查本机资料目录权限。') from None
            locks.append(lock)
        yield
    finally:
        for lock in reversed(locks):
            lock.release()
