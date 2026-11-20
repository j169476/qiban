"""Encrypted conversation originals; retention never deletes conversation evidence.

Transport workers use a current scope grant and cancellation guard. Management
methods are internal, reached only through the authenticated desktop dispatcher.
No model-facing file paths, arbitrary URLs, or unscoped content lookup are offered.
"""
import hashlib
import re
import secrets
import time
from dataclasses import asdict

from . import events
from .errors import AssistantError
from .governance import GovernanceRepository
from .reply_authorization import ReplyAuthorizations

SETTINGS = 'media-original-retention-v1'
CAPACITY = 'media-original-capacity-v1'
MIB = 1024 * 1024
MAX_BYTES = 10 * 1024 * 1024
PRESERVE = frozenset({'saved', 'favorite', 'emoticon'})


def check(condition, code='media_invalid', message='附件资料无效或已改变，请重新读取。'):
    if not condition:
        raise AssistantError(code, message)


def library_bytes(db):
    current = sum(int(db.execute("SELECT COALESCE(SUM(size),0) FROM " + table).fetchone()[0])
                  for table in ("sticker_library", "file_library", "library_version_bodies"))
    versions = db.execute("SELECT COALESCE(SUM(octet_length(metadata)),0) FROM library_versions").fetchone()[0]
    return current + int(versions)


class MediaOriginals:
    def __init__(self, store, *, clock=None):
        self.store, self.clock = store, clock or time.time

    def capacity_settings(self):
        value = self.store.state(CAPACITY, {'mib': 2048, 'revision': None})
        check(isinstance(value, dict) and set(value) == {'mib', 'revision'} and
              type(value['mib']) is int and 1 <= value['mib'] <= 1048576, 'media_settings_invalid')
        return value

    def configure_capacity(self, mib, expected, *, actor):
        check(type(mib) is int and 1 <= mib <= 1048576,
              message='附件总容量须为 1 至 1048576 MiB。')
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.capacity_settings()
            check(old['revision'] == expected, 'edit_conflict')
            value = {'mib': mib, 'revision': secrets.token_hex(16)}
            self.store.set_state(CAPACITY, value, db)
            GovernanceRepository(self.store).audit(actor, 'media_capacity_changed', CAPACITY,
                                                  old['revision'], value['revision'], db=db)
        return value

    def capacity(self):
        with self.store.db() as db:
            row = db.execute('''SELECT COALESCE(SUM(size),0) AS used,
                COALESCE(SUM(CASE WHEN preserved OR processed_at IS NULL OR
                    EXISTS(SELECT 1 FROM media_original_holds h WHERE h.asset=m.id)
                    THEN size ELSE 0 END),0) AS protected
                FROM media_originals m WHERE status='available' ''').fetchone()
            library = library_bytes(db)
            row = {**dict(row), 'used': row['used'] + library, 'protected': row['protected'] + library}
            settings = self.capacity_settings()
            return {**settings, 'used_bytes': int(row['used']), 'protected_bytes': int(row['protected']),
                    'reclaimable_bytes': int(row['used'] - row['protected']),
                    'over_limit': row['used'] > settings['mib'] * MIB}

    def _expire(self, db, row, reason):
        revision = secrets.token_hex(16)
        metadata = self.store.ledger_decode('media-original:' + row['id'], row['metadata'])
        metadata['cleanup_reason'] = reason
        db.execute("UPDATE media_originals SET body=NULL,status='expired',revision=?,metadata=?,expired_at=? WHERE id=?",
                   (revision, self.store.ledger_encode('media-original:' + row['id'], metadata), self.clock(), row['id']))
        GovernanceRepository(self.store).audit('media-' + reason, 'media_original_expired', row['id'],
                                              row['revision'], revision, db=db)

    def _make_room(self, db, incoming, guard, *, limit=None):
        used = db.execute("SELECT COALESCE(SUM(size),0) FROM media_originals WHERE status='available'").fetchone()[0]
        used += library_bytes(db)
        needed = used + incoming - self.capacity_settings()['mib'] * MIB
        removed = 0
        while needed > 0 and (limit is None or removed < limit):
            guard()
            rows = db.execute('''SELECT id,size,revision,metadata FROM media_originals m
                WHERE status='available' AND size>0 AND NOT preserved AND processed_at IS NOT NULL
                AND NOT EXISTS(SELECT 1 FROM media_original_holds h WHERE h.asset=m.id)
                ORDER BY processed_at,id LIMIT ? FOR UPDATE''',
                (100 if limit is None else min(100, limit - removed),)).fetchall()
            if not rows:
                break
            for row in rows:
                self._expire(db, row, 'capacity')
                needed -= row['size']
                removed += 1
                if needed <= 0:
                    break
        # Admission must be atomic with reclamation. Failure rolls back all
        # deletions and audits, leaving the old originals intact.
        if incoming:
            check(needed <= 0, 'media_capacity_full', '附件空间不足且现有原件受保护，本轮未保存新附件；文字回复可继续。')
        return removed

    def settings(self):
        value = self.store.state(SETTINGS, {'days': 30, 'revision': None})
        check(isinstance(value, dict) and set(value) == {'days', 'revision'} and
              type(value['days']) is int and 1 <= value['days'] <= 3650, 'media_settings_invalid')
        return value

    def configure(self, days, expected, *, actor):
        check(type(days) is int and 1 <= days <= 3650, message='附件保留期限须为 1 至 3650 天。')
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.settings()
            check(old['revision'] == expected, 'edit_conflict')
            value = {'days': days, 'revision': secrets.token_hex(16)}
            self.store.set_state(SETTINGS, value, db)
            GovernanceRepository(self.store).audit(actor, 'media_retention_changed', SETTINGS,
                                                  old['revision'], value['revision'], db=db)
        return value

    def _source(self, scope, event_id, source_version, identifier):
        current = events.get(self.store, event_id)
        value = current['event']
        check(current['version'] == source_version and value['platform'] == scope.platform
              and value['account'] == scope.account_identity and value['conversation'] == scope.kind + ':' + scope.target_id,
              'media_source_changed')
        matches = [item for item in value.get('attachments', []) if item.get('id') == identifier]
        check(len(matches) == 1 and matches[0].get('kind') in {'file', 'image', 'audio'}, 'media_source_changed')
        return matches[0]

    def put(self, scope, authorization, event_id, source_version, identifier, content, *, name, guard):
        check(isinstance(identifier, str) and re.fullmatch(r'qq-native-attachment-[a-f0-9]{40}', identifier))
        check(isinstance(content, bytes) and len(content) <= MAX_BYTES, 'media_limit')
        check(isinstance(name, str) and len(name) <= 512 and '\x00' not in name)
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            guard()
            ReplyAuthorizations(self.store).check(scope, authorization, db=db)
            attachment = self._source(scope, event_id, source_version, identifier)
            sha = hashlib.sha256(content).hexdigest()
            old = db.execute('SELECT * FROM media_originals WHERE id=? FOR UPDATE', (identifier,)).fetchone()
            if old:
                check(old['scope'] == scope.id and old['event_id'] == event_id and
                      old['source_version'] == source_version and old['sha256'] == sha, 'media_source_changed')
                check(old['status'] == 'available', 'media_expired', '附件原件已按保留规则清理。')
                self._decode(old)
                guard()
                return self._summary(old)
            # This includes protected/pending originals in the capacity count.
            # It never evicts them to satisfy an incoming attachment.
            self._make_room(db, len(content), guard)
            now = self.clock()
            receipt = db.execute('SELECT version FROM core_event_receipts WHERE event_id=?', (event_id,)).fetchone()
            consumed = self.store.state('merged-consumed:' + event_id, {}, db)
            processed = now if (receipt and receipt[0] == source_version
                                or consumed.get('version') == source_version) else None
            metadata = {'scope': asdict(scope), 'kind': attachment['kind'], 'name': name, 'preserve': []}
            revision = secrets.token_hex(16)
            db.execute('''INSERT INTO media_originals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (identifier, scope.id, event_id, source_version, sha, len(content),
                        self.store.ledger_encode('media-original:' + identifier, metadata),
                        self.store.cipher.encrypt(content, 'media-original', identifier, sha), revision,
                        now, processed, False, 'available'))
            guard()
            row = db.execute('SELECT * FROM media_originals WHERE id=?', (identifier,)).fetchone()
            return self._summary(row)

    def _decode(self, row):
        check(row['status'] == 'available', 'media_expired', '附件原件已按保留规则清理，聊天记录仍保留。')
        raw = self.store.cipher.decrypt(bytes(row['body']), 'media-original', row['id'], row['sha256'])
        check(len(raw) == row['size'] and hashlib.sha256(raw).hexdigest() == row['sha256'], 'storage_integrity')
        return raw

    def read(self, scope, authorization, identifier, *, guard):
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            guard()
            ReplyAuthorizations(self.store).check(scope, authorization, db=db)
            row = db.execute('SELECT * FROM media_originals WHERE id=?', (identifier,)).fetchone()
            check(row is not None and row['scope'] == scope.id, 'access_denied')
            self._source(scope, row['event_id'], row['source_version'], identifier)
            content = self._decode(row)
            guard()
            return content

    def _summary(self, row):
        meta = self.store.ledger_decode('media-original:' + row['id'], row['metadata'])
        return {'id': row['id'], 'revision': row['revision'], 'kind': meta['kind'], 'name': meta['name'],
                'scope': meta['scope'], 'preserve': meta['preserve'], 'bytes': row['size'],
                'cleanup_reason': meta.get('cleanup_reason'),
                'status': row['status'], 'processed_at': row['processed_at'], 'created_at': row['created_at']}

    def listing(self, after='', limit=50):
        check(isinstance(after, str) and len(after) <= 128 and type(limit) is int and 1 <= limit <= 100)
        with self.store.db() as db:
            rows = db.execute('''SELECT id,revision,metadata,size,status,processed_at,created_at
                FROM media_originals WHERE id>? ORDER BY id LIMIT ?''', (after, limit))
            return {'items': [self._summary(row) for row in rows], 'settings': self.settings()}

    def preserve(self, identifier, reasons, expected, *, actor):
        check(isinstance(identifier, str) and isinstance(reasons, list) and len(reasons) <= 3
              and all(isinstance(item, str) and item in PRESERVE for item in reasons) and len(set(reasons)) == len(reasons))
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM media_originals WHERE id=? FOR UPDATE', (identifier,)).fetchone()
            check(row is not None and row['revision'] == expected, 'edit_conflict')
            check(row['status'] == 'available', 'media_expired', '附件原件已清理，不能再设为长期保留。')
            meta = self.store.ledger_decode('media-original:' + identifier, row['metadata'])
            meta['preserve'] = sorted(reasons)
            revision = secrets.token_hex(16)
            db.execute('UPDATE media_originals SET preserved=?,metadata=?,revision=? WHERE id=?',
                       (bool(reasons), self.store.ledger_encode('media-original:' + identifier, meta), revision, identifier))
            GovernanceRepository(self.store).audit(actor, 'media_preservation_changed', identifier,
                                                  expected, revision, db=db)
            return self._summary(db.execute('SELECT * FROM media_originals WHERE id=?', (identifier,)).fetchone())

    def hold(self, owner, task, identifiers):
        """Trusted task admission; keep originals until this task explicitly exits.

        A crash does not turn a hold into a timed lease. The task supervisor must
        establish that the old owner has exited before releasing its holds.
        """
        check(all(isinstance(v, str) and 0 < len(v) <= 128 for v in (owner, task)))
        check(isinstance(identifiers, (list, tuple)) and len(identifiers) <= 36)
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for identifier in identifiers:
                row = db.execute('SELECT status FROM media_originals WHERE id=? FOR UPDATE', (identifier,)).fetchone()
                check(row is not None and row[0] == 'available', 'media_expired')
                db.execute('INSERT INTO media_original_holds VALUES(?,?,?) ON CONFLICT DO NOTHING', (owner, task, identifier))

    def release(self, owner, task):
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM media_original_holds WHERE owner=? AND task=?', (owner, task))

    def cleanup(self, *, limit=100, guard=lambda: None):
        from .process_scope import process_exited

        check(type(limit) is int and 1 <= limit <= 100)
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            guard()
            # Only holders explicitly bound to an OS process use automatic
            # recovery. Alive/unknown owners and other hold protocols stay put.
            owners = db.execute("SELECT DISTINCT owner FROM media_original_holds WHERE owner LIKE 'qiban-media:%'").fetchall()
            for holder in owners:
                match = re.fullmatch(r'qiban-media:([1-9][0-9]{0,9}):.+', holder[0])
                if match and int(match[1]) <= 0xFFFFFFFF and process_exited(int(match[1])):
                    guard()
                    db.execute('DELETE FROM media_original_holds WHERE owner=?', (holder[0],))
            threshold = self.clock() - self.settings()['days'] * 86400
            rows = db.execute('''SELECT id,revision,metadata FROM media_originals m WHERE status='available'
                AND NOT preserved AND processed_at IS NOT NULL AND processed_at<=?
                AND NOT EXISTS(SELECT 1 FROM media_original_holds h WHERE h.asset=m.id)
                ORDER BY processed_at,id LIMIT ? FOR UPDATE''', (threshold, limit)).fetchall()
            for row in rows:
                self._expire(db, row, 'retention')
            reclaimed = self._make_room(db, 0, guard, limit=limit-len(rows))
            guard()
            return {'expired': len(rows) + reclaimed}


def verify_originals(store):
    """Restore validation uses ciphertext and projection consistency, no downloads."""
    media = MediaOriginals(store)
    media.settings()
    media.capacity_settings()
    with store.db() as db, db.stream("SELECT * FROM media_originals") as rows:
        for row in rows:
            summary = media._summary(row)
            from .reply_authorization import ReplyScope

            check(ReplyScope(**summary['scope']).id == row['scope'] and summary['kind'] in {'file', 'audio', 'image'}
                  and isinstance(summary['preserve'], list) and set(summary['preserve']) <= PRESERVE
                  and bool(summary['preserve']) == row['preserved'], 'backup_invalid')
            if row['body'] is not None:
                media._decode(row)
