"""Trusted local preservation provider for the desktop portable-restore channel.

Every portable replacement keeps its pre-replacement copy under
``<store>/history/portable-preservation`` and never deletes one: the original generation's
store, and every ready restored generation's store which happens to be selected or not.
This provider enumerates exactly those artifacts for the private native channel. All
addresses are derived from the installation root plus validated identifiers and file names;
a caller supplies neither a path, a generation name nor a credential slot. Nothing here
follows a symbolic link, a junction or another reparse point, decrypts anything while
listing, or holds key material between calls.

Listing is metadata only and paginated: one page costs a directory scan and per-file
``lstat``, never a snapshot decryption, and a ``next`` cursor continues from the last entry
that was shown so no retained copy becomes unreachable. The selected artifact is
authenticated later, by ``DesktopPortable.preserved_preview`` and again at completion.

Candidates that exist but cannot be trusted (a link, a directory, an over-size file) stay in
the listing with ``usable=False`` instead of being silently dropped or read; a generation
without a valid, ready manifest of this installation is not enumerated at all, because no
trusted store address exists for it.

Only trusted host code (``storage_worker.portable_host``) builds this; the page reaches it
through ``DesktopPortable.preserved_list`` and ``preserved_preview``.

Refusals are AssistantError with a stable code: preserved_unavailable, preserved_handle,
preserved_missing and preserved_changed.
"""
from __future__ import annotations

import os
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path

import installation_generation

from companion_core import postgres_transfer
from companion_core.errors import AssistantError
from qiban.desktop_portable import PRESERVATION

PAGE = 20
MAX_PAGE = 50
MAX_HANDLES = 1024
MAX_NAME = 200
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
STALE = '这份本机保全资料列表已失效，请重新读取'
VANISHED = '这份本机保全资料已不存在，请重新读取列表'
CHANGED = '这份本机保全资料文件已改变，已拒绝读取'
NO_VAULT = '本机凭据不可用，暂时无法读取这份保全资料'


def _reparse(path):
    """A link, junction or other reparse point, tested by path inspection only."""
    try:
        details = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, 'st_file_attributes', 0) & REPARSE)


class PreservedProvider:
    """Opaque handles over the retained pre-replacement copies of one installation."""

    def __init__(self, root, installation, key_for_slot, *, page=PAGE):
        if not isinstance(installation, str) or not 0 < len(installation) <= 200 or not callable(key_for_slot):
            raise AssistantError('preserved_unavailable', '本机保全资料暂不可用')
        self.installation_root = Path(root)
        self.installation, self.key_for_slot = installation, key_for_slot
        self.page_size = page if type(page) is int and 0 < page <= MAX_PAGE else PAGE
        self.handles, self.tokens = {}, {}
        self.cursors = {}

    # -- listing ----------------------------------------------------------------
    def page(self, after=''):
        """One bounded page of retained copies, newest file time first."""
        cursor = self._cursor(after)
        ordered = sorted(self._candidates(), key=lambda item: item['order'], reverse=True)
        if cursor is not None:
            ordered = [item for item in ordered if item['order'] < cursor]
        shown = ordered[:self.page_size]
        # The cursor is the position of the last entry shown, so a copy that disappears
        # between two pages can neither duplicate nor hide the entries that follow it.
        following = shown[-1]['order'] if len(ordered) > self.page_size else None
        return {'items': [self._public(item) for item in shown],
                'next': self._cursor_token(following) if following is not None else ''}

    def _candidates(self):
        """Every retained copy of the original generation and every ready generation."""
        root = self.root()
        for keyref, store in self._stores(root):
            folder = self._folder(store)
            if folder is None:
                continue
            for name, details, usable in self._files(folder):
                yield {'keyref': keyref, 'name': name,
                       # Unique order: the same file name can exist in the original store and
                       # in a generation with an identical file time, and one cursor must never
                       # swallow the other copy.
                       'order': (details.st_mtime_ns, keyref, name),
                       'taken_at': datetime.fromtimestamp(details.st_mtime, timezone.utc).isoformat(),
                       'bytes': int(details.st_size), 'usable': usable}

    def _stores(self, root):
        original = installation_generation._derived(root, installation_generation.STORE)
        if original.is_dir() and not _reparse(original):
            yield self.installation, original
        folder = installation_generation._derived(root, installation_generation.GENERATIONS)
        if not folder.is_dir() or _reparse(folder):
            return
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return
        for name in names:
            if not installation_generation.IDENTIFIER.fullmatch(name):
                continue
            try:
                installation_generation._manifest(root, self.installation, name)
                store = installation_generation._store(
                    root, installation_generation.GENERATIONS, name, installation_generation.STORE)
            except AssistantError:
                # Not a ready generation of this installation: there is no trusted address.
                continue
            yield name, store

    def _folder(self, store):
        """This generation's preservation folder, or None when it holds no reachable copy."""
        try:
            folder = installation_generation._derived(store, 'history', PRESERVATION)
        except AssistantError:
            return None
        return folder if folder.is_dir() and not _reparse(folder) else None

    def _files(self, folder):
        try:
            with os.scandir(folder) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if not name.endswith('.ccbackup') or len(name) > MAX_NAME or '/' in name or '\\' in name:
                continue
            try:
                details = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            usable = (not entry.is_symlink() and not _reparse(entry.path)
                      and stat.S_ISREG(details.st_mode)
                      and 0 < details.st_size <= postgres_transfer.MAX_BACKUP)
            yield name, details, usable

    def _public(self, item):
        return {'handle': self._handle(item), 'name': item['name'],
                'taken_at': item['taken_at'], 'bytes': item['bytes'], 'usable': item['usable']}

    # -- handles ----------------------------------------------------------------
    def _handle(self, item):
        key = (item['keyref'], item['name'])
        token = self.tokens.get(key)
        if token is None:
            token = secrets.token_urlsafe(24)
            self.tokens[key] = token
            self.handles[token] = {'keyref': item['keyref'], 'name': item['name'],
                                   'taken_at': item['taken_at'], 'usable': item['usable']}
            while len(self.handles) > MAX_HANDLES:
                oldest, dropped = next(iter(self.handles.items()))
                self.handles.pop(oldest)
                self.tokens.pop((dropped['keyref'], dropped['name']), None)
        else:
            entry = self.handles.get(token)
            if entry is not None:
                entry.update(taken_at=item['taken_at'], usable=item['usable'])
        return token

    def _entry(self, handle):
        if not isinstance(handle, str) or not handle:
            raise AssistantError('preserved_handle', STALE)
        entry = self.handles.get(handle)
        if entry is None:
            raise AssistantError('preserved_handle', STALE)
        return entry

    def _cursor(self, after):
        if after is None or after == '':
            return None
        if not isinstance(after, str) or after not in self.cursors:
            raise AssistantError('preserved_handle', STALE)
        return self.cursors[after]

    def _cursor_token(self, order):
        token = secrets.token_urlsafe(24)
        self.cursors[token] = order
        while len(self.cursors) > MAX_HANDLES:
            self.cursors.pop(next(iter(self.cursors)))
        return token

    # -- reading one selected copy ----------------------------------------------
    def open(self, handle):
        """Bytes and data key of one selected copy; the caller authenticates the payload."""
        entry = self._entry(handle)
        if not entry['usable']:
            raise AssistantError('preserved_changed', CHANGED)
        blob, key = self._read(entry['keyref'], entry['name'])
        return {'name': entry['name'], 'keyref': entry['keyref'], 'taken_at': entry['taken_at'],
                'key': key, 'blob': blob}

    def reopen(self, keyref, name):
        """Read the same copy again for completion; the address is derived, never stored."""
        blob, key = self._read(keyref, name)
        return {'name': name, 'keyref': keyref, 'key': key, 'blob': blob}

    def _read(self, keyref, name):
        path = self._path(keyref, name)
        try:
            details = os.lstat(path)
        except OSError:
            raise AssistantError('preserved_missing', VANISHED) from None
        if (stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode)
                or getattr(details, 'st_file_attributes', 0) & REPARSE):
            raise AssistantError('preserved_changed', CHANGED)
        if not 0 < details.st_size <= postgres_transfer.MAX_BACKUP:
            raise AssistantError('preserved_changed', CHANGED)
        try:
            with path.open('rb') as stream:
                raw = stream.read(postgres_transfer.MAX_BACKUP + 1)
        except OSError:
            raise AssistantError('preserved_missing', VANISHED) from None
        if len(raw) != details.st_size:
            raise AssistantError('preserved_changed', CHANGED)
        return raw, self._key(keyref)

    def _key(self, keyref):
        try:
            key = self.key_for_slot(keyref)
        except AssistantError:
            raise AssistantError('preserved_unavailable', NO_VAULT) from None
        except Exception:
            raise AssistantError('preserved_unavailable', NO_VAULT) from None
        if not isinstance(key, bytes) or len(key) != 32:
            raise AssistantError('preserved_unavailable', NO_VAULT)
        return key

    def _path(self, keyref, name):
        if (not isinstance(name, str) or not 0 < len(name) <= MAX_NAME or '/' in name
                or '\\' in name or ':' in name or not name.endswith('.ccbackup')):
            raise AssistantError('preserved_handle', STALE)
        store = self._store(keyref)
        folder = installation_generation._derived(store, 'history', PRESERVATION)
        path = folder/name
        try:
            inside = path.resolve().parent == folder.resolve()
        except OSError:
            inside = False
        if not inside:
            raise AssistantError('preserved_changed', CHANGED)
        return path

    def _store(self, keyref):
        root = self.root()
        if keyref == self.installation:
            try:
                return installation_generation._store(root, installation_generation.STORE)
            except AssistantError:
                raise AssistantError('preserved_missing', '本机原始资料目录不可用') from None
        if not isinstance(keyref, str) or not installation_generation.IDENTIFIER.fullmatch(keyref):
            raise AssistantError('preserved_handle', STALE)
        try:
            installation_generation._manifest(root, self.installation, keyref)
            return installation_generation._store(
                root, installation_generation.GENERATIONS, keyref, installation_generation.STORE)
        except AssistantError:
            raise AssistantError('preserved_handle', STALE) from None

    def root(self):
        try:
            return installation_generation._root(self.installation_root)
        except AssistantError:
            raise AssistantError('preserved_unavailable', '本机资料目录不可用，无法读取保全资料') from None


def preserved_provider(root, installation, key_for_slot, *, page=PAGE):
    """Build the provider this installation's private channel uses for retained copies."""
    return PreservedProvider(root, installation, key_for_slot, page=page)
