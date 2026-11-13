"""Source-bound native downloads into encrypted originals; no model invocation.

Only a trusted task may call this with a selected candidate and a byte allowance.
It reopens the received range, resolves current native locators, and compares the
canonical event before downloading. Returned data contains no native file path.
"""
import hashlib
import re
from pathlib import Path

from companion_core import events
from companion_core.contracts import Event
from companion_core.media_originals import MAX_BYTES, MIB, MediaOriginals
from companion_core.reply_authorization import ReplyAuthorizations, require

from .native_normalization import normalize_native


class NativeMedia:
    def __init__(self, provider, module):
        require(provider.store is module.store, 'qq_store_mismatch')
        self.provider, self.module, self.store = provider, module, module.store

    def save(self, candidate, *, max_bytes, owner, task, guard):
        require(type(max_bytes) is int and 0 < max_bytes <= MAX_BYTES, 'qq_media_limit')
        saved = events.get(self.store, candidate['event_id'])
        require(saved['version'] == candidate['source_version'], 'media_source_changed')
        event = Event.from_dict(saved['event'])
        attachment = next((a for a in event.attachments if a.get('id') == candidate['id']), None)
        require(attachment is not None and attachment['kind'] == candidate['kind'], 'media_source_changed')
        scope = self.module.scope(event)
        permission = ReplyAuthorizations(self.store).version(scope)
        require(permission is not None, 'reply_not_authorized')

        def check():
            guard()
            self.module.validate_context_event(event)
            ReplyAuthorizations(self.store).check(scope, permission)
            require(not self.provider.closed, 'qq_native_disconnected')

        media = MediaOriginals(self.store)
        check()
        # Reuse a verified original without consulting native cache locators.
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM media_originals WHERE id=?', (candidate['id'],)).fetchone()
            if row is not None:
                require(row['size'] <= max_bytes, 'qq_media_limit')
                media.hold(owner, task, [candidate['id']])
                media.read(scope, permission, candidate['id'], guard=check)
                return media._summary(row)

        with self.provider._poll_lock:
            check()
            self.provider.identity()
            snapshot = self.module.native_inbox.state(scope.kind, scope.target_id)
            require(snapshot is not None and snapshot['complete'], 'qq_media_source_unavailable')
            peer = (self.provider.verified['friends'][scope.target_id] if scope.kind == 'private' else scope.target_id)
            require(snapshot['owner'] == {'uin': self.provider.verified['uin'],
                                          'uid': self.provider.verified['uid'], 'peer': peer}, 'qq_native_owner_changed')
            worker = self.provider.worker
            window = worker.request(scope.kind + '_open',
                                    **{'group' if scope.kind == 'group' else 'target': scope.target_id,
                                       'lower': snapshot['lower'], 'upper': snapshot['upper']})
            require(isinstance(window, dict) and window.get('upper') == snapshot['upper']
                    and type(window.get('count')) is int and 0 <= window['count'] <= 10000
                    and isinstance(window.get('window_id'), str), 'qq_native_page_invalid')
            match = None
            for page in range(max(1, (window['count'] + 99) // 100)):
                check()
                packet = worker.request(scope.kind + '_page', window_id=window['window_id'], page=page)
                require(packet.get('window_id') == window['window_id'] and packet.get('kind') == scope.kind
                        and packet.get('target') == scope.target_id and packet.get('page') == page
                        and packet.get('lower') == snapshot['lower'] and packet.get('upper') == snapshot['upper']
                        and isinstance(packet.get('messages'), list)
                        and len(packet['messages']) == min(100, window['count'] - page*100), 'qq_native_page_invalid')
                for offset, raw in enumerate(packet['messages']):
                    canonical = normalize_native(raw, self.module.options, identity=self.provider.verified,
                        sent_reference=lambda kind, target, identifier:
                            self.store.state(self.module.sent_key(kind, target, identifier)) is not None)
                    if canonical is not None and canonical.id == event.id:
                        require(canonical == event and match is None, 'media_source_changed')
                        match = (page*100 + offset, raw)
            require(match is not None, 'qq_media_source_unavailable')
            index, raw = match
            position = attachment['position']
            part = raw['elements'][position]
            field = {'image': 'picElement', 'audio': 'pttElement', 'file': 'fileElement'}[attachment['kind']]
            meta = part[field]
            declared = meta.get('fileSize')
            if isinstance(declared, str) and re.fullmatch(r'[0-9]{1,20}', declared):
                declared = int(declared)
            require(type(declared) is int and 0 <= declared <= max_bytes, 'qq_media_limit')
            capacity = media.capacity()
            require(capacity['protected_bytes'] + declared <= capacity['mib'] * MIB, 'media_capacity_full')
            check()
            result = worker.request('download_attachment', window_id=window['window_id'], index=index,
                position=position, max_bytes=max_bytes, source={'msg_id': raw['msgId'], 'time': raw['msgTime'],
                    'sender_uid': raw['senderUid'], 'element_id': part['elementId']})
            # The worker published a random, independent verified copy. Validate
            # ownership before reading or deleting; SDK cache paths never leave
            # this boundary and are never accepted as the copied original.
            root = worker.media_root
            require(isinstance(root, Path), 'qq_media_path')
            path = Path(result.get('path', ''))
            require(path.is_absolute() and path.parent == root and re.fullmatch(r'[a-f0-9]{32}\.bin', path.name)
                    and not root.is_symlink() and root.resolve(strict=True) == root
                    and not path.is_symlink() and path.resolve(strict=True) == path and path.is_file(), 'qq_media_path')
            try:
                check()
                require(result.get('kind') == attachment['kind'], 'qq_media_integrity')
                with path.open('rb') as stream:
                    content = stream.read(max_bytes + 1)
                require(len(content) <= max_bytes and len(content) == result.get('bytes')
                        and hashlib.sha256(content).hexdigest() == result.get('sha256'), 'qq_media_integrity')
                name = meta.get('fileName') if attachment['kind'] == 'file' else ''
                name = name if isinstance(name, str) else ''
                with self.store.lock, self.store.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    value = media.put(scope, permission, event.id, saved['version'], candidate['id'], content,
                                      name=name, guard=check)
                    media.hold(owner, task, [candidate['id']])
                    check()
                return value
            finally:
                # Only the validated independent .bin copy; never cache folders.
                path.unlink(missing_ok=True)
