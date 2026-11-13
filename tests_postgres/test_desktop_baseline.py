import uuid

import pytest

from companion_core import baseline
from companion_core.errors import AssistantError
from qiban.desktop_management import DesktopManagement
from tests_postgres.test_desktop_management import call, desktop_store, service

__all__=['desktop_store','service']


def expected(service):
    value=call(service,'baseline.preview')['result']
    return {key:value[key] for key in ('pins','foundation_count')}


def test_publish_receipt_survives_restart_without_republishing(service):
    preview=expected(service)
    assert not baseline.enabled(service.store)
    key=uuid.uuid4().hex
    result=call(service,'baseline.publish',{'expected':preview},key)
    assert result['ok'] and result['result']['shared'] and not result['result']['role_started']
    assert service.repository.policy('persona/@config').classification=='public'
    reopened=DesktopManagement(service.store,service.installation,service.windows_sid)
    assert call(reopened,'baseline.publish',{'expected':preview},key)==result
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM governance_audit WHERE operation='publish_baseline'").fetchone()[0]==1


def test_stale_preview_and_active_role_cannot_publish(service):
    preview=expected(service)
    card=service.store.read('persona')
    service.admin.patch_config(service.request(),'persona',{'name':'新名字'},card['version'])
    assert call(service,'baseline.publish',{'expected':preview})['error']=='edit_conflict'
    service.store.set_state('core-run:qq',{'status':'active'})
    assert call(service,'baseline.publish',{'expected':expected(service)})['error']=='baseline_active'
    assert not baseline.enabled(service.store)


def test_publish_and_receipt_roll_back_together(service,monkeypatch):
    preview=expected(service)
    before=service.repository.policy('persona/@config')
    save=service.store.set_state
    def fail(key,*args,**kwargs):
        if key.startswith('desktop-request:'):
            raise AssistantError('synthetic_failure','test')
        return save(key,*args,**kwargs)
    monkeypatch.setattr(service.store,'set_state',fail)
    assert call(service,'baseline.publish',{'expected':preview})['error']=='synthetic_failure'
    assert not baseline.enabled(service.store)
    assert service.repository.policy('persona/@config')==before


def test_lock_and_untrusted_admin_flags_cannot_publish(service):
    preview=expected(service)
    service.enable_lock('Synthetic-Baseline-Lock-2026!')
    service.lock()
    assert call(service,'baseline.preview')['error']=='desktop_locked'
    assert call(service,'baseline.publish',{'expected':preview})['error']=='desktop_locked'
    assert not call(service,'baseline.publish',{'expected':preview},administrator=True)['ok']
    assert not baseline.enabled(service.store)


def test_add_and_remove_keep_existing_baseline_policy(service):
    assert call(service,'baseline.publish',{'expected':expected(service)})['ok']
    card=service.store.read('persona')
    assert call(service,'foundation.add',{'identifier':'desktop-foundation','content':'可公开的合成设定🙂',
                                        'expected':card['version']})['ok']
    memory=service.store.read('memories','desktop-foundation')
    assert memory['data']['locked'] and memory['data']['source']['type']=='user_instruction'
    assert service.repository.policy('memories/desktop-foundation').classification=='public'
    card=service.store.read('persona')
    assert call(service,'config.patch',{'kind':'persona','changes':{'foundation_memories':[]},'expected':card['version']})['ok']
    assert service.store.exists('memories','desktop-foundation')
    assert not service.repository.policy('memories/desktop-foundation').valid


@pytest.mark.parametrize('value',[None,[],{}, {'pins':[],'foundation_count':True},
                                 {'pins':[{'kind':'memories'}],'foundation_count':1}])
def test_invalid_preview_is_rejected_without_side_effects(service,value):
    assert call(service,'baseline.publish',{'expected':value})['error']=='management_request'
    assert not baseline.enabled(service.store)
