"""Real core and encrypted ledger with a synthetic supervised operator."""
from datetime import datetime, timezone

import pytest

from companion_core.runtime import Companion
from companion_wechat.contracts import digest
from companion_wechat.gateway import PermitAuthority
from companion_wechat.live_sender import DriverOutcome
from companion_wechat.receiver import DatabaseGateway
from companion_wechat.role import RoleAdapter, RoleSession
from companion_wechat.supervised_sender import StagedProof, SupervisedSenderGateway
from tests_postgres.test_wechat_role import Host, Model
from tests_wechat.test_receiver import STAMP, Factory, Worker, batch, row, snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmed', [True, False])
async def test_supervised_core_two_targets_once_and_encrypted_journal(pg_store, confirmed):
    class Operator:
        def __init__(self):
            self.prepared, self.granted, self.closed = [], [], False

        async def prepare(self, request):
            self.prepared.append(request.action_id)
            return StagedProof(request.account_identity, request.target_ref, 'synthetic-native-incarnation',
                               'synthetic-observed-draft-' + request.conversation, digest(request.text),
                               observed=True, draft_matches=True, manual_takeover=False)

        async def dispatch_once(self, request, proof, *, authorize):
            grant = authorize()
            assert grant['expires_unix_ms'] > 0
            record = pg_store.state(sender._action_key(request))
            assert record['status'] == 'submitting' and record['grant_issued'] is True
            self.granted.append(request.action_id)
            return DriverOutcome('confirmed' if confirmed else 'uncertain', client_recorded=confirmed)

        async def close(self):
            self.closed = True

    worker = Worker([snapshot()])
    receiver = DatabaseGateway(pg_store, {}, account='synthetic-supervised-core', transport_factory=Factory(worker))
    await receiver.start()
    now = datetime.fromtimestamp(STAMP, timezone.utc)
    session = RoleSession(pg_store, receiver.account, clock=lambda: now)
    permits = PermitAuthority(b'synthetic-private-permit-key-1234', clock=lambda: 0.0)
    operator = Operator()
    sender = SupervisedSenderGateway(pg_store, receiver, permits, operator, session)
    adapter = RoleAdapter(receiver, sender, permits, session)
    model = Model()
    core = Companion(pg_store, model, Host(), [adapter], clock=lambda: now, run_key='core-run:wechat')
    try:
        core.start()
        core.guard()
        response, _after = batch(receiver._saved,
                                 row(1, text=session.nonce + ' synthetic private'),
                                 row(1, group=True, text=session.nonce + ' synthetic family'))
        worker.responses.append(response)
        admitted = await adapter.collect_once()
        assert {event.conversation for event in admitted} == {'owner_main', 'family'}
        for event in admitted:
            result = await core.handle(event)
            assert result['action']['status'] == ('succeeded' if confirmed else 'uncertain')
            assert (await core.handle(event))['duplicate'] is True
        assert len(model.calls) == len(operator.prepared) == len(operator.granted) == 2
        with pg_store.db() as db:
            actions = db.execute('SELECT status,body FROM core_actions').fetchall()
            assert len(actions) == 2 and all(item['body'].startswith('enc1:') for item in actions)
            assert all(item['status'] == ('succeeded' if confirmed else 'uncertain') for item in actions)
    finally:
        core.stop('synthetic_supervised_complete')
        await adapter.close()
    assert receiver._closed and worker.closed and operator.closed
