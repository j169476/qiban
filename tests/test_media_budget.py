import hashlib
import json

import pytest

from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.media_budget import DEFAULTS, MIB, TurnMediaBudget, select, unread_notice, validate


def rows(count=8, *, group=False, trigger=None, kind='image', text='附件'):
    result = []
    for i in range(count):
        event = Event('qq', '10001', str(i), ActorRef('qq', '10002', '10001'),
                      'group:10003' if group else 'private:10002',
                      f'2026-10-31T00:{i // 60:02d}:{i % 60:02d}+00:00', text,
                      'group' if group else 'private',
                      attachments=({'id': f'asset-{i}', 'kind': kind, 'position': 0},),
                      metadata={'qq': {'identity_quality': 'verified', 'trigger': 'mention' if i == trigger else 'none'}})
        body = event.as_dict()
        result.append({'event': body, 'version': hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()})
    return result


def test_trigger_priority_then_recent_and_omission_notice():
    plan = select(rows(group=True, trigger=0), DEFAULTS)
    assert [p['id'] for p in plan['selected']] == ['asset-0', 'asset-7', 'asset-6']
    assert plan['omitted'] == {'image_count': 5}
    notice = unread_notice(plan)
    assert notice['read'] == 0 and notice['unread'] == 8
    assert [r['code'] for r in notice['reasons']] == ['image_count', 'unavailable']


def test_flood_is_text_bounded_before_any_attachment_work():
    plan = select(rows(500, group=True, trigger=0, text='水' * 1000), DEFAULTS)
    assert plan['selected'][0]['id'] == 'asset-0'
    assert len(plan['selected']) == 3 and plan['omitted']['context_limit'] > 400
    assert len(json.dumps(unread_notice(plan))) < 3000
    assert sum(plan['omitted'].values()) + len(plan['selected']) == 500


def test_zero_disables_type_and_cross_scope_is_rejected():
    assert select(rows(), {**DEFAULTS, 'images': 0})['selected'] == []
    with pytest.raises(AssistantError) as error:
        select(rows(1) + rows(1, group=True), DEFAULTS)
    assert error.value.code == 'backlog_scope'


@pytest.mark.parametrize('field,value', [('images', True), ('audio', -1), ('files', 13),
                                        ('single_mib', 11), ('total_mib', 0), ('audio_seconds', 601)])
def test_invalid_limits(field, value):
    with pytest.raises(AssistantError):
        validate({**DEFAULTS, field: value})


def test_actual_bytes_are_charged_across_whole_turn():
    plan = select(rows(), DEFAULTS)
    budget = TurnMediaBudget(plan)
    for item in plan['selected'][:2]:
        lease = budget.reserve(item, declared_bytes=1)  # Metadata can understate size.
        budget.complete(lease, actual_bytes=10 * MIB)
    assert budget.reserve(plan['selected'][2], declared_bytes=1) == {'accepted': False, 'reason': 'total_bytes'}


def test_remaining_capacity_is_passed_as_hard_download_limit():
    plan = select(rows(), DEFAULTS)
    budget = TurnMediaBudget(plan)
    for item in plan['selected'][:2]:
        lease = budget.reserve(item)
        budget.complete(lease, actual_bytes=8 * MIB)
    lease = budget.reserve(plan['selected'][2])
    assert lease['max_bytes'] == 4 * MIB
    with pytest.raises(AssistantError) as error:
        budget.complete(lease, actual_bytes=4 * MIB + 1)
    assert error.value.code == 'media_budget_exceeded'
    assert budget.used_bytes == 20 * MIB


def test_audio_uses_decoded_duration_not_declared_duration():
    plan = select(rows(3, kind='audio'), DEFAULTS)
    assert plan['omitted'] == {'audio_count': 1}
    budget = TurnMediaBudget(plan)
    first = budget.reserve(plan['selected'][0], declared_bytes=20, declared_audio_ms=1000)
    budget.complete(first, actual_bytes=20, actual_audio_ms=90000)
    second = budget.reserve(plan['selected'][1])
    assert second['max_audio_ms'] == 30000
    with pytest.raises(AssistantError):
        budget.complete(second, actual_bytes=20, actual_audio_ms=30001)


def test_known_oversize_skips_before_download_and_unknown_has_bounded_lease():
    plan = select(rows(), DEFAULTS)
    budget = TurnMediaBudget(plan)
    assert budget.reserve(plan['selected'][0], declared_bytes=10*MIB+1)['reason'] == 'single_bytes'
    lease = budget.reserve(plan['selected'][1])
    assert lease['max_bytes'] == 10*MIB
    with pytest.raises(AssistantError):
        budget.reserve(plan['selected'][2])  # No overlapping unaccounted downloads.
    budget.failed(lease)
    assert budget.used_bytes == 10*MIB
    with pytest.raises(AssistantError):
        budget.reserve(plan['selected'][1])  # No retry/refund within this reply.


def test_source_version_and_lease_cannot_be_replaced():
    plan = select(rows(), DEFAULTS)
    budget = TurnMediaBudget(plan)
    item = plan['selected'][0]
    with pytest.raises(AssistantError):
        budget.reserve({**item, 'source_version': 'changed'})
    lease = budget.reserve(item)
    with pytest.raises(AssistantError):
        budget.complete({**lease, 'max_bytes': 30*MIB}, actual_bytes=25*MIB)
    budget.complete(lease, actual_bytes=1)


def test_exact_byte_and_audio_boundaries_are_accepted():
    plan = select(rows(1, kind='audio'), DEFAULTS)
    budget = TurnMediaBudget(plan)
    lease = budget.reserve(plan['selected'][0], declared_bytes=10*MIB, declared_audio_ms=120000)
    budget.complete(lease, actual_bytes=10*MIB, actual_audio_ms=120000)
    assert budget.used_audio_ms == 120000


def test_audio_cannot_be_published_without_measured_duration():
    plan = select(rows(1, kind='audio'), DEFAULTS)
    budget = TurnMediaBudget(plan)
    lease = budget.reserve(plan['selected'][0])
    with pytest.raises(AssistantError):
        budget.complete(lease, actual_bytes=20)
    assert budget.used_audio_ms == 120000
