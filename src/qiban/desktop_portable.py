"""Portable export and cross-installation restore preview for the native management host.

Only the host's private pipe calls `dispatch()`; nothing here is registered as a
management operation or a model tool. Export writes one `.ccbackup` artifact and one
independent-password `.cckey` beside it without ever replacing an existing file,
preview authenticates both files plus this installation's preservation backup, and
complete hands the verified pair, together with the trusted source name the preview
kept (a retained copy's file name or the import's own backup file name), to an injected
stager. This module never copies rows, never switches a database and never reports a
switch the stager did not report.

The same private channel also serves this machine's own retained copies
(`history/portable-preservation`): `preserved_list` pages their metadata through an
injected trusted provider and `preserved_preview` authenticates one selected copy with
the data key of the generation that wrote it, without any recovery password. One
preview slot, one pending challenge and one unwrapped source key are shared by both
kinds of source, so only one replacement can ever be armed at a time.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path

import yaml

from companion_core import portable_validation, postgres_transfer
from companion_core.cipher import ContentCipher
from companion_core.errors import AssistantError
from companion_core.storage import StrictLoader
from companion_service import recovery

from .desktop_identity import OWNER

BACKUP_SUFFIX = '.ccbackup'
KEY_SUFFIX = '.cckey'
PRESERVATION = 'portable-preservation'
BACKUP_INFO = b'companion-postgres-backup-v1'
MAX_BACKUP = postgres_transfer.MAX_BACKUP
MAX_KEY_FILE = 300
PREVIEW_TTL = 600
CHALLENGE_TTL = 180
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
UNCERTAIN = '切换步骤已提交但结果未知，请按宿主状态确认，不要重复提交'
EFFECT = ('将完整替换当前资料与历史，并接续备份中的本机使用者与角色逻辑身份；两份资料不合并；'
          '保留当前 Windows 管理验证与应用密码锁；恢复后角色保持停止，平台授权需重新检查。')
PRESERVED_EFFECT = ('将完整替换当前资料与历史，并接续这份本机保全资料中的使用者与角色逻辑身份；'
                    '两份资料不合并；当前资料会先另存为新的本机保全备份；保留当前 Windows 管理验证'
                    '与应用密码锁；恢复后角色保持停止，平台授权需重新检查。')


def _publish(temporary, target):
    """Publish `temporary` as `target` without replacing it; True when the temp was consumed."""
    try:
        os.link(temporary, target)  # atomic and no-clobber wherever hard links work
    except FileExistsError:
        raise
    except OSError:
        if os.name != 'nt':
            raise
        os.rename(temporary, target)  # Windows rename refuses an existing target
        return True
    return False


def _discard(temporary):
    """Remove a temporary file after publishing; separate from publishing so failures show."""
    os.unlink(temporary)


def _unwrap(blob, password):
    """Decrypt the exported data key; integrity here means the password or key file is wrong."""
    try:
        return recovery.unwrap(blob, password)
    except AssistantError as error:
        if error.code == 'storage_integrity':
            raise AssistantError('recovery_key', '恢复口令或密钥文件不匹配，请重新核对') from None
        raise


def _unpack(blob, backup_key):
    """Open the snapshot; integrity here means the artifact is damaged or the pair is wrong."""
    try:
        return postgres_transfer.unpack(blob, backup_key)
    except AssistantError as error:
        if error.code == 'storage_integrity':
            raise AssistantError('backup_invalid', '备份文件已损坏或与密钥文件不匹配') from None
        raise


def _switched(report):
    """The stager's success contract; anything less is an unknown outcome."""
    return (isinstance(report, dict) and report.get('switched') is True
            and report.get('restored') is True and type(report.get('restart_required')) is bool)


class DesktopPortable:
    """Export, preview and verified hand-off for one portable restore pair."""

    def __init__(self, management, key_provider, stager, clock=time.time, preserved=None):
        if not callable(key_provider) or not callable(stager):
            raise AssistantError('portable_host', '便携恢复宿主尚未就绪')
        self.management, self.store = management, management.store
        self.clock, self.key_provider, self.stager = clock, key_provider, stager
        self.preview_state = self.pending = None
        self.source_key = None
        # Trusted local-preservation provider; because host code builds it, the page can
        # only name an opaque handle this installation already enumerated.
        self.preserved = preserved

    # -- shared management rules -------------------------------------------------
    def require(self):
        try:
            request = self.management.request()
            self.management.authority.require(request, administrator=True)
        except AssistantError:
            # A locked or ended management session must not keep unwrapped key material.
            self.reset()
            raise
        return request

    def paused(self):
        """Same pause guard as the local restore: no busy tasks and no active run."""
        backups = self.management.backups
        if backups is None:
            raise AssistantError('portable_unavailable', '备份服务尚未就绪')
        backups._paused()  # shared rule; this module adds no second copy of it

    def reset(self):
        self.preview_state = self.pending = None
        self.source_key = None

    def _key(self):
        key = self.key_provider()
        if not isinstance(key, bytes) or len(key) != 32:
            raise AssistantError('portable_key', '本机资料密钥不可用')
        return key

    @staticmethod
    def _backup_key(key):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=BACKUP_INFO).derive(key)

    # -- export ------------------------------------------------------------------
    @staticmethod
    def _targets(path):
        if not isinstance(path, str) or not path or path != path.strip():
            raise AssistantError('portable_path', '请选择备份保存位置')
        target = Path(path)
        if not target.is_absolute() or target.suffix.lower() != BACKUP_SUFFIX:
            raise AssistantError('portable_path', '备份文件必须是完整本地路径并以 .ccbackup 结尾')
        key_file = target.with_suffix(KEY_SUFFIX)
        parent = target.parent
        if not parent.is_dir() or parent.is_symlink():
            raise AssistantError('portable_path', '备份保存目录不可用')
        for candidate in (target, key_file):
            if candidate.exists() or candidate.is_symlink():
                raise AssistantError('portable_exists', '目标文件已存在，不会覆盖')
        return target, key_file

    @staticmethod
    def _remove(paths):
        """Remove only what this call created; True when nothing of ours is left."""
        remaining = False
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            remaining = remaining or path.exists() or path.is_symlink()
        return not remaining

    @classmethod
    def _write_pair(cls, entries):
        """Write every artifact to a unique temporary file, then publish all of them."""
        temps, created, failed = [], [], None
        try:
            for target, payload in entries:
                temporary = target.with_name('.' + target.name + '.' + secrets.token_hex(8) + '.tmp')
                stream = temporary.open('xb')  # never treat a name we did not create as ours
                temps.append(temporary)
                with stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            for (target, _), temporary in zip(entries, tuple(temps)):
                consumed = _publish(temporary, target)
                created.append(target)
                if not consumed:
                    _discard(temporary)
                    temps.remove(temporary)
            return created
        except OSError as error:
            failed = error
        removed = cls._remove([*temps, *created])
        if isinstance(failed, FileExistsError):
            raise AssistantError('portable_exists', '目标文件已存在，不会覆盖') from None
        raise AssistantError('portable_write',
                             '无法写入备份文件，本次创建的文件已删除' if removed
                             else '无法写入备份文件，且未能删除本次创建的文件，请手动检查') from None

    def export(self, path, password):
        self.require()
        target, key_file = self._targets(path)
        key = self._key()
        # The independent password is validated before any file exists.
        key_blob = recovery.wrap(key, password)
        backup_key = self._backup_key(key)
        blob = postgres_transfer.create(self.store, backup_key)
        with self.store.db() as db:
            records = db.execute('SELECT COUNT(*) FROM content_records').fetchone()[0]
            history = db.execute('SELECT COUNT(*) FROM content_history').fetchone()[0]
        created = self._write_pair([(target, blob), (key_file, key_blob)])
        try:
            # The published files themselves are read back; a pair is only reported
            # after both artifacts authenticate from disk.
            _, backup_bytes = self._read(str(target), MAX_BACKUP, '备份文件')
            _, key_bytes = self._read(str(key_file), MAX_KEY_FILE, '恢复密钥文件')
            if not (hmac.compare_digest(hashlib.sha256(backup_bytes).hexdigest(),
                                        hashlib.sha256(blob).hexdigest())
                    and hmac.compare_digest(hashlib.sha256(key_bytes).hexdigest(),
                                            hashlib.sha256(key_blob).hexdigest())):
                raise ValueError()
            _unpack(backup_bytes, backup_key)
            if not hmac.compare_digest(_unwrap(key_bytes, password), key):
                raise ValueError()
        except (AssistantError, ValueError):
            removed = self._remove(created)
            raise AssistantError('portable_write',
                                 '备份文件校验失败，本次创建的文件已删除' if removed
                                 else '备份文件校验失败，且未能删除本次创建的文件，请手动检查') from None
        return {'backup': target.name, 'key_file': key_file.name,
                'backup_sha256': hashlib.sha256(backup_bytes).hexdigest(),
                'key_sha256': hashlib.sha256(key_bytes).hexdigest(),
                'backup_bytes': len(backup_bytes), 'key_bytes': len(key_bytes),
                'records': records, 'history': history, 'encrypted': True}

    # -- preview -----------------------------------------------------------------
    @staticmethod
    def _read(path, limit, label):
        if not isinstance(path, str) or not path or path != path.strip():
            raise AssistantError('portable_path', '请选择' + label)
        file = Path(path)
        if not file.is_absolute():
            raise AssistantError('portable_path', label + '必须是完整本地路径')
        try:
            entry = file.lstat()
        except OSError:
            raise AssistantError('portable_missing', label + '不存在或无法读取') from None
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            raise AssistantError('portable_path', label + '必须是普通本地文件')
        if getattr(entry, 'st_file_attributes', 0) & REPARSE:
            raise AssistantError('portable_path', label + '不能是链接或重解析点')
        if not 0 < entry.st_size <= limit:
            raise AssistantError('portable_limit', label + '大小超出允许范围')
        try:
            with file.open('rb') as stream:
                data = stream.read(limit + 1)
        except OSError:
            raise AssistantError('portable_missing', label + '无法读取') from None
        if len(data) != entry.st_size or len(data) > limit:
            raise AssistantError('portable_changed', label + '在读取期间发生变化')
        return file, data

    def _compatible(self, snapshot):
        tables = postgres_transfer.tables(self.store)
        if snapshot.get('format') != 1 or set(snapshot.get('tables', {})) != set(tables):
            raise AssistantError('portable_schema', '备份资料结构不是当前新版，旧的 0.9.4 资料不受支持')
        with self.store.db() as db:
            migrations = [list(row) for row in
                          db.execute('SELECT version,checksum FROM schema_migrations ORDER BY version')]
        if snapshot.get('migrations') != migrations:
            raise AssistantError('portable_schema', '备份数据库版本与当前应用不一致，仅支持当前新版资料')
        for table in tables:
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', table):
                raise AssistantError('portable_schema', '备份字段不匹配')
            with self.store.db() as db:
                cursor = db.execute('SELECT * FROM "' + table + '" LIMIT 0')
                columns = [column.name for column in cursor.cursor.description]
            if snapshot['tables'][table].get('columns') != columns:
                raise AssistantError('portable_schema', '备份字段与当前应用版本不一致')

    def _preserve(self):
        """Keep a locally recoverable copy without touching the bounded backup history.

        `PostgresStore.backup` prunes to `policy.backup_keep`, so preservation writes its
        own artifact under `history/portable-preservation` and never removes any file.
        """
        backup_key = self._backup_key(self._key())
        blob = postgres_transfer.create(self.store, backup_key)
        folder = self.store.root / 'history' / PRESERVATION
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (secrets.token_hex(16) + BACKUP_SUFFIX)
        created = self._write_pair([(target, blob)])
        try:
            _, stored = self._read(str(target), MAX_BACKUP, '保全备份')
            if not hmac.compare_digest(hashlib.sha256(stored).hexdigest(), hashlib.sha256(blob).hexdigest()):
                raise ValueError()
            _unpack(stored, backup_key)
        except (AssistantError, ValueError):
            removed = self._remove(created)
            raise AssistantError('portable_preserve',
                                 '恢复前保全备份校验失败，本次文件已删除' if removed
                                 else '恢复前保全备份校验失败，且未能删除本次文件，请手动检查') from None
        return {'name': target.name, 'sha256': hashlib.sha256(stored).hexdigest(), 'bytes': len(stored)}

    # -- source labels -----------------------------------------------------------
    @staticmethod
    def _state(snapshot, cipher, key):
        block = snapshot['tables']['state']
        index = {name: position for position, name in enumerate(block['columns'])}
        for row in block['rows']:
            if row[index['key']] != key:
                continue
            value = row[index['value']]
            if not isinstance(value, str) or not value.startswith('enc1:'):
                raise AssistantError('portable_source', '备份中的来源安装记录格式无效')
            try:
                return json.loads(cipher.decrypt(base64.b64decode(value[5:], validate=True),
                                                 'ledger', 'state:' + key, 'v1'))
            except (AssistantError, ValueError, TypeError):
                raise AssistantError('portable_source', '备份中的来源安装记录无法读取') from None
        raise AssistantError('portable_source', '备份中缺少来源安装记录')

    @staticmethod
    def _record(snapshot, cipher, kind):
        """Decode one config record: the stored plaintext is strict YAML (not JSON)."""
        block = snapshot['tables']['content_records']
        index = {name: position for position, name in enumerate(block['columns'])}
        for row in block['rows']:
            if row[index['kind']] != kind or row[index['id']] != '@config':
                continue
            body = row[index['body']]
            if not isinstance(body, dict) or set(body) != {'$bytes'}:
                raise AssistantError('portable_source', '备份中的来源资料格式无效')
            try:
                plain = cipher.decrypt(base64.b64decode(body['$bytes'], validate=True),
                                       kind, '@config', row[index['version']])
                data = yaml.load(plain.decode('utf-8'), Loader=StrictLoader)
            except (AssistantError, ValueError, UnicodeError, yaml.YAMLError):
                raise AssistantError('portable_source', '备份中的来源资料无法读取') from None
            if not isinstance(data, dict):
                raise AssistantError('portable_source', '备份中的来源资料格式无效')
            return data
        raise AssistantError('portable_source', '备份中缺少来源资料')

    def _labels(self, snapshot, key):
        """Display labels decoded from authenticated payloads; never silently blank."""
        cipher = ContentCipher(key)
        manifest = self._record(snapshot, cipher, 'manifest')
        persona = self._record(snapshot, cipher, 'persona')
        owner = self._state(snapshot, cipher, 'desktop-owner')
        project, name, installation = manifest.get('project_id'), persona.get('name'), owner.get('installation')
        if not isinstance(project, str) or not project:
            raise AssistantError('portable_source', '备份中的来源项目标识缺失')
        if not isinstance(name, str):
            raise AssistantError('portable_source', '备份中的角色资料格式无效')
        if not isinstance(installation, str) or not installation:
            raise AssistantError('portable_source', '备份不是来自可识别的栖伴安装')
        return {'project': project[:12], 'role': name, 'installation': installation[:12]}

    def _verified(self, backup_bytes, source_key):
        """Open one snapshot and authenticate every registered encrypted payload.

        The caller adds the schema comparison where a preview must show it; completion
        relies on the stager's own check so one rule is never enforced twice.
        """
        snapshot = _unpack(backup_bytes, self._backup_key(source_key))
        counters = portable_validation.validate_snapshot(snapshot, source_key, self.store.profile)
        return snapshot, counters

    def _arm(self, request, source_key, counters, details):
        """Preserve this store, take one fingerprint and arm the single preview slot."""
        with self.store.lock:
            # Preservation and the fingerprint must describe one consistent state.
            preservation = self._preserve()
            fingerprint = postgres_transfer.fingerprint(self.store)
        self.source_key = source_key
        self.preview_state = {'token': secrets.token_urlsafe(32), 'session': request.session_id,
                              'expires': self.clock() + PREVIEW_TTL, 'expected': fingerprint,
                              'preservation': preservation, 'counters': counters, **details}
        return self.preview_state

    def preview(self, backup, key_file, password):
        request = self.require()
        self.paused()
        self.reset()
        backup_path, backup_bytes = self._read(backup, MAX_BACKUP, '备份文件')
        key_path, key_bytes = self._read(key_file, MAX_KEY_FILE, '恢复密钥文件')
        source_key = _unwrap(key_bytes, password)
        snapshot, counters = self._verified(backup_bytes, source_key)
        self._compatible(snapshot)
        labels = self._labels(snapshot, source_key)
        entry = self._arm(request, source_key, counters, {
            'mode': 'portable', 'backup': str(backup_path),
            'backup_sha256': hashlib.sha256(backup_bytes).hexdigest(),
            'key_file': str(key_path),
            'key_sha256': hashlib.sha256(key_bytes).hexdigest()})
        return {'token': entry['token'], 'expires_in_seconds': PREVIEW_TTL,
                'records': counters['current_records'], 'history': counters['history_records'],
                'counters': counters, 'source': labels, 'kind': 'portable',
                'preservation': {'name': entry['preservation']['name'],
                                 'sha256': entry['preservation']['sha256']},
                'effect': EFFECT}

    # -- this machine's own retained copies --------------------------------------
    def _provider(self):
        if self.preserved is None:
            raise AssistantError('preserved_unavailable', '本机保全资料暂不可用')
        return self.preserved

    def _same_installation(self, snapshot, source_key):
        """A local copy must belong to this installation and this physical owner.

        Only this machine writes such a copy, so a different owner means the file came
        from somewhere else; adopting it would replace the physical identity.
        """
        owner = self._state(snapshot, ContentCipher(source_key), OWNER)
        current = self.store.state(OWNER)
        if (not isinstance(owner, dict) or not isinstance(current, dict)
                or not isinstance(owner.get('installation'), str)
                or owner.get('installation') != current.get('installation')
                or owner.get('actor') != current.get('actor')):
            raise AssistantError('preserved_source', '这份本机保全资料不属于当前安装，无法在本机恢复')

    def preserved_list(self, after=''):
        """One page of retained copies: file name, file time and size, nothing decrypted."""
        self.require()
        return self._provider().page(after)

    def preserved_preview(self, handle):
        request = self.require()
        self.paused()
        self.reset()
        provider = self._provider()
        source = provider.open(handle)
        snapshot, counters = self._verified(source['blob'], source['key'])
        self._compatible(snapshot)
        labels = self._labels(snapshot, source['key'])
        self._same_installation(snapshot, source['key'])
        entry = self._arm(request, source['key'], counters, {
            'mode': 'preserved', 'keyref': source['keyref'], 'name': source['name'],
            'taken_at': source['taken_at'],
            'source_sha256': hashlib.sha256(source['blob']).hexdigest()})
        return {'token': entry['token'], 'expires_in_seconds': PREVIEW_TTL,
                'records': counters['current_records'], 'history': counters['history_records'],
                'source': labels, 'kind': 'preserved', 'name': source['name'],
                'taken_at': source['taken_at'],
                'preservation': {'name': entry['preservation']['name'],
                                 'sha256': entry['preservation']['sha256']},
                'effect': PRESERVED_EFFECT}

    # -- challenge hand-off ------------------------------------------------------
    def begin(self, token):
        request = self.require()
        self.paused()
        entry, self.preview_state = self.preview_state, None
        if (not entry or not isinstance(token, str) or not hmac.compare_digest(entry['token'], token)
                or entry['session'] != request.session_id or entry['expires'] <= self.clock()):
            self.reset()
            raise AssistantError('portable_preview_expired', '便携恢复预览已失效，请重新查看')
        challenge = secrets.token_urlsafe(32)
        self.pending = {**entry, 'challenge': challenge, 'expires': self.clock() + CHALLENGE_TTL}
        return {'challenge': challenge, 'expires_in_seconds': CHALLENGE_TTL}

    def _source_bytes(self, entry, source_key, preserved):
        """Re-read exactly what the preview authenticated; a change is never carried over."""
        if preserved:
            source = self._provider().reopen(entry['keyref'], entry['name'])
            if not (hmac.compare_digest(hashlib.sha256(source['blob']).hexdigest(), entry['source_sha256'])
                    and hmac.compare_digest(source['key'], source_key)):
                raise AssistantError('preserved_changed', '本机保全资料在预览后已改变，请重新选择')
            return source['blob']
        _, backup_bytes = self._read(entry['backup'], MAX_BACKUP, '备份文件')
        _, key_bytes = self._read(entry['key_file'], MAX_KEY_FILE, '恢复密钥文件')
        if not (hmac.compare_digest(hashlib.sha256(backup_bytes).hexdigest(), entry['backup_sha256'])
                and hmac.compare_digest(hashlib.sha256(key_bytes).hexdigest(), entry['key_sha256'])):
            raise AssistantError('portable_changed', '备份或密钥文件在预览后已改变，请重新检查')
        return backup_bytes

    def complete(self, challenge):
        request = self.require()
        entry, self.pending = self.pending, None
        if (not entry or not isinstance(challenge, str) or not hmac.compare_digest(entry['challenge'], challenge)
                or entry['session'] != request.session_id or entry['expires'] <= self.clock()):
            self.reset()
            raise AssistantError('portable_verification_expired', '便携恢复验证已失效，请重新预览并验证 Windows 身份')
        source_key = self.source_key
        if source_key is None:
            self.reset()
            raise AssistantError('portable_preview_expired', '便携恢复预览已失效，请重新查看')
        preserved = entry.get('mode') == 'preserved'
        try:
            self.paused()
            backup_bytes = self._source_bytes(entry, source_key, preserved)
            self._verified(backup_bytes, source_key)
            if postgres_transfer.fingerprint(self.store) != entry['expected']:
                raise AssistantError('portable_changed', '当前资料在预览后已变化，请重新检查')
            # The preview already authenticated where this restore comes from: a retained
            # local copy keeps its own file name, a portable import its own backup file
            # name. That trusted basename is handed to the stager as the source name; it is
            # never read back from the page, and the stager still reports an unnamed source
            # as unnamed rather than reusing the preservation copy.
            source_name = entry['name'] if preserved else Path(entry['backup']).name
            target = {'source_name': source_name}
            if preserved:
                target['source_mode'] = 'preserved'
            try:
                report = self.stager(source_blob=backup_bytes, source_key=source_key,
                                     expected_current_fingerprint=entry['expected'],
                                     preservation=dict(entry['preservation']), **target)
                if not _switched(report):
                    raise ValueError()
            except Exception:
                # Never retried and never reported as "nothing changed": only the host
                # can say whether the switch was applied.
                raise AssistantError('portable_switch_uncertain', UNCERTAIN) from None
        finally:
            self.reset()
        return {'stager': report, 'switched': True, 'restored': True,
                'restart_required': report['restart_required'], 'preservation': entry['preservation']}

    def cancel(self):
        self.require()
        self.reset()
        return {'cancelled': True}

    # -- native host channel -----------------------------------------------------
    def dispatch(self, message):
        """Private native-host channel; page and model paths cannot reach these operations."""
        handlers = {'export': ({'path', 'password'}, self.export),
                    'preview': ({'backup', 'key_file', 'password'}, self.preview),
                    'preserved-list': ({'after'}, self.preserved_list),
                    'preserved-preview': ({'handle'}, self.preserved_preview),
                    'begin': ({'token'}, self.begin), 'complete': ({'challenge'}, self.complete),
                    'cancel': (set(), self.cancel)}
        try:
            if (not isinstance(message, dict) or set(message) != {'action', 'operation', 'arguments'}
                    or message['action'] != 'desktop-portable'
                    or not isinstance(message['operation'], str)
                    or not isinstance(message['arguments'], dict)):
                raise AssistantError('management_request', '便携恢复请求格式无效')
            operation, arguments = message['operation'], message['arguments']
            if operation not in handlers or set(arguments) != handlers[operation][0]:
                raise AssistantError('management_request', '便携恢复请求格式无效')
            return {'ok': True, 'result': handlers[operation][1](**arguments)}
        except AssistantError as error:
            return {'ok': False, 'error': error.code, 'message': str(error)}
        except Exception:
            # Fixed code and message: no password, key, path, digest or payload escapes.
            return {'ok': False, 'error': 'portable_failed', 'message': '便携恢复请求未能完成，请重试或查看诊断'}
