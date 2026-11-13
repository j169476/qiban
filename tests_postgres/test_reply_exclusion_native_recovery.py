"""Independent actual NativeProvider/NativeReceiver exclusion acceptance."""
import pytest

from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.reply_exclusion import ReplyExclusions
from tests_postgres.test_qq_module import STAMP
from tests_postgres.test_qq_module import env as generic_env  # noqa: F401
from tests_postgres.test_qq_native_provider import native as native

__all__ = ['generic_env', 'native']


def change(env, excluded):
    scope = ReplyScope('qq', env.module.account, 'group', '10003')
    repo = ReplyExclusions(env.store, clock=lambda: STAMP - (2 if excluded else 1))
    return repo.update(scope, excluded=excluded, expected_version=repo.record(scope)['version'], actor='independent-test')


async def test_manual_native_receiver_survives_lift_and_replies_new_input(native):
    env = native
    assert await env.module.runtime_poll() == []
    worker = env.workers[-1]
    change(env, True)
    assert await env.module.runtime_poll() == []
    change(env, False)
    worker.arrivals()
    assert await env.module.runtime_poll() == []
    pending = await env.module.runtime_poll()
    assert len(pending) == 1
    assert (await env.core.handle(pending[0]))['action']['status'] == 'succeeded'
    assert not worker.closed and worker.submits == 1


@pytest.mark.parametrize('command', ['group_head', 'group_open', 'group_page'])
async def test_exclusion_during_native_read_does_not_close_account(native, command):
    env = native
    assert await env.module.runtime_poll() == []
    worker = env.workers[-1]
    worker.arrivals()
    original = worker.request
    triggered = []

    def request(name, **args):
        value = original(name, **args)
        if name == command and not triggered:
            triggered.append(name)
            change(env, True)
        return value

    worker.request = request
    assert await env.module.runtime_poll() == []
    assert triggered == [command]
    assert not worker.closed and env.module.ready
    change(env, False)
    for _ in range(4):
        pending = await env.module.runtime_poll()
        if pending:
            assert len(pending) == 1
            assert (await env.core.handle(pending[0]))['action']['status'] == 'succeeded'
            break
    else:
        pytest.fail('Native receiver never resumed after exclusion was lifted')
    assert worker.submits == 1 and not worker.closed


async def test_replayed_committed_native_page_keeps_identity_across_lift(generic_env):
    from tests.test_qq_native_normalization import IDENTITY
    from tests_postgres.test_qq_native_windows import message, packet
    env = generic_env
    source = packet([message(2, mention=True)], complete=True, upper=2)
    receipt = env.module.accept_native_page(source, identity=IDENTITY)
    scope = ReplyScope('qq', env.module.account, 'group', '10003')
    repo = ReplyExclusions(env.store, clock=lambda: STAMP + 1)
    blocked = repo.update(scope, excluded=True, expected_version=None, actor='independent-test')
    repo.clock = lambda: STAMP + 2
    repo.update(scope, excluded=False, expected_version=blocked['version'], actor='independent-test')
    # A committed source page retried after receiver interruption is identical;
    # changing eligibility must not change its identity or recreate a reply.
    assert env.module.accept_native_page(source, identity=IDENTITY) == receipt
    assert await env.module.runtime_poll() == []


async def test_never_excluded_receiver_keeps_existing_manual_owner_convention(native):
    from companion_core.reply_audience import Audience
    from tests_postgres.test_reply_audiences import set_mode
    env = native
    category = Audience('qq', env.module.account, 'group')
    set_mode(env.store, category, True)
    set_mode(env.store, category, False)
    assert await env.module.runtime_poll() == []
    scope = ReplyScope('qq', env.module.account, 'group', '10003')
    # This is the owner format produced before AUTH164 after switching back to
    # manual selection. It must remain valid for a scope never excluded.
    key = env.module.provider.receiver.key('group', '10003')
    saved = env.store.state(key)
    expected = ReplyAuthorizations(env.store).version(scope)
    env.store.set_state(key, {**saved, 'owner': {**saved['owner'], 'authorization': expected}})
    assert await env.module.runtime_poll() == []
    assert env.module.ready and not env.workers[-1].closed
