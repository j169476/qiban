"""Logical local-user and role identity bound to this installation, not a second owner.

`desktop-owner` and the management actor always stay the current physical
installation's Windows administrator; the application lock and Windows verification
are unchanged. A restored snapshot can legitimately carry another installation's
local user and role identities, so at most one such pair is bound to this
installation: the private room then reads and writes the same historical actor and
the runtime keeps the restored role, while management rights are never granted to
either.

Trusted restore, staging and test code only. Nothing here is reachable from a
management operation or a model tool. A present but malformed binding is refused;
it is never treated as "not bound" and never silently repaired or widened.
"""
from __future__ import annotations

import re

from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository
from companion_core.storage import utcnow

BINDING = 'desktop-logical-identity'
OWNER = 'desktop-owner'
ROLE = 'desktop-role'
FORMAT = 1
OWNER_PLATFORM = 'local'
ROOM_PLATFORM = 'local'
ROLE_PLATFORM = 'qiban-role'
KEYS = frozenset({'format', 'installation', 'owner', 'project_id', 'room', 'logical_role',
                  'source', 'bound_at'})
SOURCE_KEYS = frozenset({'installation', 'kit', 'at'})
SHAPE = frozenset({'platform', 'user_id', 'scope'})
INVALID = '本机逻辑身份绑定与当前安装不一致，请检查安装或恢复结果'


def _text(value, label):
    if not isinstance(value, str) or not 0 < len(value) <= 200 or not re.fullmatch(r'[!-~]+', value):
        raise AssistantError('desktop_identity_invalid', label + '无效')
    return value


def _actor(value, platform, label):
    if isinstance(value, ActorRef):
        value = value.as_dict()
    if not isinstance(value, dict) or set(value) != SHAPE:
        raise AssistantError('desktop_identity_invalid', label + '格式无效')
    if value['platform'] != platform:
        raise AssistantError('desktop_identity_invalid', label + '平台不符')
    _text(value['user_id'], label)
    _text(value['scope'], label)
    try:
        return ActorRef(**value)
    except AssistantError:
        raise AssistantError('desktop_identity_invalid', label + '无效') from None


def _source(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not set(value) <= SOURCE_KEYS:
        raise AssistantError('desktop_identity_invalid', '恢复来源记录无效')
    return {key: _text(item, '恢复来源记录') for key, item in value.items()}


def project_id(store):
    data = store.read('manifest')['data']
    value = data.get('project_id')
    if not isinstance(value, str) or not value:
        raise AssistantError('desktop_identity_invalid', '本机资料标识缺失')
    return value


def current_role(store, db=None):
    """The installed role as an ActorRef; a corrupt value is refused, never ignored."""
    value = store.state(ROLE, None, db)
    if value is None:
        return None
    try:
        return _actor(value, ROLE_PLATFORM, '本机角色身份')
    except AssistantError:
        raise AssistantError('desktop_identity_invalid', INVALID) from None


def current_owner(store, db=None):
    value = store.state(OWNER, None, db)
    if not isinstance(value, dict) or not isinstance(value.get('actor'), dict):
        raise AssistantError('desktop_identity_invalid', '本机管理员身份无效')
    return value


def binding(store, db=None):
    """Raw binding value: None means unbound; any other type must validate as a record."""
    return store.state(BINDING, None, db)


def _record(value, installation=None, owner=None):
    """Validate one binding record; cross-check the live installation when supplied."""
    if not isinstance(value, dict) or set(value) != KEYS or value.get('format') != FORMAT:
        raise AssistantError('desktop_identity_invalid', INVALID)
    _text(value['installation'], '本机安装标识')
    _text(value['project_id'], '本机资料标识')
    parsed = {'installation': value['installation'], 'project_id': value['project_id'],
              'owner': _actor(value['owner'], OWNER_PLATFORM, '本机管理员身份'),
              'room': _actor(value['room'], ROOM_PLATFORM, '会客室逻辑身份'),
              'logical_role': _actor(value['logical_role'], ROLE_PLATFORM, '角色逻辑身份'),
              'source': _source(value['source']), 'bound_at': _text(value['bound_at'], '绑定时间')}
    if installation is not None and parsed['installation'] != installation:
        raise AssistantError('desktop_identity_invalid', INVALID)
    if owner is not None and parsed['owner'] != owner:
        raise AssistantError('desktop_identity_invalid', INVALID)
    return parsed


def verify(store, *, installation, owner, db=None):
    """The bound identities, or None when this installation genuinely has no binding."""
    value = binding(store, db)
    if value is None:
        return None
    parsed = _record(value, installation=installation, owner=owner)
    # The installed role is exactly the restored one; there is no second physical copy.
    if current_role(store, db) != parsed['logical_role']:
        raise AssistantError('desktop_identity_invalid', INVALID)
    if parsed['project_id'] != project_id(store):
        raise AssistantError('desktop_identity_invalid', INVALID)
    return parsed


def resolve(store, *, installation, owner, db=None):
    """Logical private-room actor, or None when this installation has no binding."""
    parsed = verify(store, installation=installation, owner=owner, db=db)
    return parsed['room'] if parsed else None


def verify_role(store, role, *, installation, owner):
    """Runtime role: the restored logical role when bound, otherwise this installation's own."""
    if not isinstance(role, dict):
        raise AssistantError('runtime_identity', '本机角色身份尚未建立')
    try:
        ref = _actor(role, ROLE_PLATFORM, '本机角色身份')
    except AssistantError:
        raise AssistantError('runtime_identity', '本机角色身份尚未建立') from None
    parsed = verify(store, installation=installation, owner=owner)
    if parsed is None:
        if ref.scope != installation:
            raise AssistantError('runtime_identity', '本机角色身份尚未建立')
        return ref
    if parsed['logical_role'] != ref:
        raise AssistantError('desktop_identity_invalid', INVALID)
    return parsed['logical_role']


def bind(store, *, installation, owner, room, logical_role, source=None, db=None):
    """Bind the restored logical identities. Trusted staging/restore path only.

    The caller stages the restored role as this installation's `desktop-role` first;
    the binding then records that same role as `logical_role`, so the room, the
    runtime and the binding can never disagree.
    """
    if not isinstance(owner, ActorRef):
        raise AssistantError('desktop_identity_invalid', '本机管理员身份无效')
    _text(installation, '本机安装标识')
    installed = current_owner(store, db)
    if installed['installation'] != installation or installed['actor'] != owner.as_dict():
        raise AssistantError('desktop_identity_invalid', '本机管理员身份与当前安装不匹配')
    room_ref = _actor(room, ROOM_PLATFORM, '会客室逻辑身份')
    role_ref = _actor(logical_role, ROLE_PLATFORM, '角色逻辑身份')
    if current_role(store, db) != role_ref:
        raise AssistantError('desktop_identity_invalid', '请先把本机角色设为接续的来源角色')
    previous = binding(store, db)
    if previous is not None:
        prior = _record(previous)
        # Repeated restores keep the historical identity; only the current binding is refreshed.
        if prior['room'] != room_ref or prior['logical_role'] != role_ref:
            raise AssistantError('desktop_identity_conflict', '已绑定的历史身份不能替换为其他来源')
    record = {'format': FORMAT, 'installation': installation, 'owner': owner.as_dict(),
              'project_id': project_id(store), 'room': room_ref.as_dict(),
              'logical_role': role_ref.as_dict(), 'source': _source(source), 'bound_at': utcnow()}
    store.set_state(BINDING, record, db)
    GovernanceRepository(store).audit(owner.key, 'desktop_identity_bound', installation, db=db)
    return record


def restore_local(store, *, role, existing, db):
    """A restore inside this installation keeps its own role and binding, or their absence.

    The snapshot may hold another installation's `desktop-role` and binding; both must
    be written back from the values read before the restore transaction copied rows.
    """
    if role is None:
        db.execute('DELETE FROM state WHERE key=?', (ROLE,))
    else:
        store.set_state(ROLE, role, db)
    if existing is None:
        db.execute('DELETE FROM state WHERE key=?', (BINDING,))
        return
    _record(existing)
    store.set_state(BINDING, existing, db)
