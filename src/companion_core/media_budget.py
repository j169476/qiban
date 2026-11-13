"""One frozen media budget per merged reply, before download or model work.

Selection is based only on persisted, scope-checked events. Native metadata is
not evidence that an attachment was read. A sequential lease bounds actual
bytes and decoded duration before those contents may enter decision material.
"""
import secrets
from collections import Counter
from copy import deepcopy

from .backlog_context import group_tail, group_trigger, snapshots
from .errors import AssistantError
from .governance import GovernanceRepository

KEY = 'media-turn-budget-v1'
MIB = 1024 * 1024
DEFAULTS = {'images': 3, 'audio': 2, 'files': 1, 'single_mib': 10, 'total_mib': 20, 'audio_seconds': 120}
BOUNDS = {'images': (0, 12), 'audio': (0, 12), 'files': (0, 12),
          'single_mib': (1, 10), 'total_mib': (1, 120), 'audio_seconds': (1, 600)}
KINDS = {'image': 'images', 'audio': 'audio', 'file': 'files'}
REASONS = {'context_limit': '所在消息超出本轮群聊文字范围', 'unsupported': '附件类型尚不支持',
           'image_count': '图片数量超过本轮上限', 'audio_count': '语音数量超过本轮上限',
           'file_count': '文件数量超过本轮上限', 'single_bytes': '单个附件超过大小上限',
           'total_bytes': '本轮附件合计大小超过上限', 'audio_duration': '语音合计时长超过上限',
           'unavailable': '附件内容尚未接通读取'}


def validate(value):
    if (not isinstance(value, dict) or set(value) != set(DEFAULTS)
            or any(type(value[k]) is not int or not low <= value[k] <= high
                   for k, (low, high) in BOUNDS.items())):
        raise AssistantError('media_budget_invalid', '附件处理上限无效，请按页面标注的范围设置整数。')
    return deepcopy(value)


class MediaBudgetSettings:
    def __init__(self, store):
        self.store = store

    def read(self):
        value = self.store.state(KEY, {'limits': DEFAULTS, 'revision': None})
        if not isinstance(value, dict) or set(value) != {'limits', 'revision'}:
            raise AssistantError('media_budget_invalid', '附件处理上限配置无效。')
        return {'limits': validate(value['limits']), 'revision': value['revision']}

    def configure(self, limits, expected, *, actor):
        limits = validate(limits)
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.read()
            if old['revision'] != expected:
                raise AssistantError('edit_conflict', '附件处理上限已改变，请重新读取。')
            value = {'limits': limits, 'revision': secrets.token_hex(16)}
            self.store.set_state(KEY, value, db)
            GovernanceRepository(self.store).audit(actor, 'media_budget_changed', KEY,
                                                   old['revision'], value['revision'], db=db)
        return value


def select(rows, limits):
    """Latest verified trigger first, then newest visible events; never fetch media.

    The result is bounded by the configurable counts, even for an attachment
    flood. Omitted references remain in source events, not an unbounded prompt.
    """
    limits = validate(limits)
    sources = snapshots(rows)
    included = {row['id'] for row in sources}
    if sources and sources[0]['event']['visibility'] == 'group':
        units, _ = group_tail(sources)
        included = {unit['id'] for unit in units}
    counts, omitted, selected, seen = Counter(), Counter(), [], set()
    priority = sorted(enumerate(sources), key=lambda pair: (bool(group_trigger(pair[1])), pair[0]), reverse=True)
    for _, row in priority:
        for attachment in row['event'].get('attachments', ()):
            kind = attachment.get('kind')
            if row['id'] not in included:
                omitted['context_limit'] += 1
                continue
            if kind not in KINDS or not isinstance(attachment.get('id'), str):
                omitted['unsupported'] += 1
                continue
            identifier = (row['id'], attachment['id'])
            if identifier in seen:
                continue
            seen.add(identifier)
            if counts[kind] >= limits[KINDS[kind]]:
                omitted[kind + '_count'] += 1
                continue
            counts[kind] += 1
            selected.append({'event_id': row['id'], 'source_version': row['version'],
                             'id': attachment['id'], 'kind': kind})
    return {'limits': limits, 'selected': selected, 'omitted': dict(omitted)}


def unread_notice(plan):
    """Truthful model material until a reader supplies verified content."""
    reasons = dict(plan['omitted'])
    if plan['selected']:
        reasons['unavailable'] = len(plan['selected'])
    return {'limits': plan['limits'], 'candidates': plan['selected'], 'read': 0,
            'unread': sum(reasons.values()),
            'reasons': [{'code': code, 'count': count, 'reason': REASONS[code]} for code, count in reasons.items()],
            'guidance': '附件候选不代表已读取；未读内容不能补猜。超限引用保留在原消息中，本轮不自动拆分调用模型。'}


def prepare_unread(store, merged, event):
    from . import events

    batch = merged.for_event(event)
    members = merged.check(batch) if batch else [event]
    plan = select([events.get(store, member.id) for member in members], MediaBudgetSettings(store).read()['limits'])
    return unread_notice(plan)


class TurnMediaBudget:
    """Sequential admission; failed/uncertain work never refunds its reservation.

    The downloader must enforce max_bytes while reading, and the decoder must
    enforce max_audio_ms while producing samples. Commit checks real output
    before publishing it. Native declarations alone cannot satisfy these limits.
    """
    def __init__(self, plan):
        self.limits = validate(plan['limits'])
        self.candidates = {(p['event_id'], p['id']): deepcopy(p) for p in plan['selected']}
        self.attempted, self.active, self.used_bytes, self.used_audio_ms = set(), None, 0, 0

    def reserve(self, candidate, *, declared_bytes=None, declared_audio_ms=None):
        key = (candidate['event_id'], candidate['id'])
        if self.active is not None or key in self.attempted or self.candidates.get(key) != candidate:
            raise AssistantError('media_budget_state', '附件处理顺序或来源已变化。')
        self.attempted.add(key)
        for value in (declared_bytes, declared_audio_ms):
            if value is not None and (type(value) is not int or value < 0):
                raise AssistantError('media_budget_invalid', '附件大小或时长无效。')
        single = self.limits['single_mib'] * MIB
        remaining = self.limits['total_mib'] * MIB - self.used_bytes
        remaining_audio = self.limits['audio_seconds'] * 1000 - self.used_audio_ms
        reason = ('single_bytes' if declared_bytes is not None and declared_bytes > single else
                  'total_bytes' if remaining <= 0 or declared_bytes is not None and declared_bytes > remaining else
                  'audio_duration' if candidate['kind'] == 'audio' and
                  (remaining_audio <= 0 or declared_audio_ms is not None and declared_audio_ms > remaining_audio) else None)
        if reason:
            return {'accepted': False, 'reason': reason}
        lease = {'accepted': True, 'token': secrets.token_hex(16), 'max_bytes': min(single, remaining),
                 'max_audio_ms': remaining_audio if candidate['kind'] == 'audio' else 0}
        self.active = deepcopy(lease)
        # Reserve the whole allowance, not an untrusted declared size. If the
        # worker dies, this allowance stays spent for the rest of this turn.
        self.used_bytes += lease['max_bytes']
        self.used_audio_ms += lease['max_audio_ms']
        return lease

    def complete(self, lease, *, actual_bytes, actual_audio_ms=0):
        if self.active != lease:
            raise AssistantError('media_budget_state', '附件处理凭据无效。')
        self.active = None
        if (type(actual_bytes) is not int or not 0 <= actual_bytes <= lease['max_bytes']
                or type(actual_audio_ms) is not int
                or not (1 if lease['max_audio_ms'] else 0) <= actual_audio_ms <= lease['max_audio_ms']):
            raise AssistantError('media_budget_exceeded', '附件实际大小或时长超过本轮上限，未提供给模型。')
        self.used_bytes -= lease['max_bytes'] - actual_bytes
        self.used_audio_ms -= lease['max_audio_ms'] - actual_audio_ms

    def failed(self, lease):
        if self.active != lease:
            raise AssistantError('media_budget_state', '附件处理凭据无效。')
        self.active = None
