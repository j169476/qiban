import pytest

from companion_core.access import AccessService, RecordPolicy, RequestAuthority
from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository
from companion_core.storage import Store
from companion_core.verification import VerificationService


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    repo = GovernanceRepository(store, clock=lambda: 1000)
    repo.initialize()
    authority = RequestAuthority(epoch=repo.epoch, clock=lambda: 1000)
    left, right = ActorRef("alpha", "1"), ActorRef("beta", "2")
    requests = [authority.issue(actor=actor, conversation=actor.key, audience="private", recipients=(actor,))
                for actor in (left, right)]
    repo.set_policy(RecordPolicy("memory/own", "1", "personal", frozenset((left.key,))), None, "test")
    return VerificationService(repo, authority), left, right, requests


def test_both_real_accounts_required_no_replay_or_transitivity(service):
    verifier, left, right, requests = service
    challenge = verifier.begin(requests[0], right, ["memory/own"])
    access = AccessService(verifier.repository, verifier.authority)
    assert not access.can_read(requests[1], "memory/own")
    assert not verifier.prove(requests[0], challenge["id"], challenge["left_code"])["verified"]
    with pytest.raises(AssistantError):
        verifier.prove(requests[0], challenge["id"], challenge["right_code"])
    assert verifier.prove(requests[1], challenge["id"], challenge["right_code"])["verified"]
    assert access.can_read(requests[1], "memory/own")
    with pytest.raises(AssistantError):
        verifier.prove(requests[1], challenge["id"], challenge["right_code"])
    third = ActorRef("gamma", "3")
    with pytest.raises(AssistantError):
        verifier.begin(requests[1], third, ["memory/own"])


def test_failed_attempts_commit_and_public_proofs_rejected(service):
    verifier, left, right, requests = service
    challenge = verifier.begin(requests[0], right, ["memory/own"])
    for _ in range(5):
        with pytest.raises(AssistantError):
            verifier.prove(requests[1], challenge["id"], "wrong")
    with pytest.raises(AssistantError):
        verifier.prove(requests[1], challenge["id"], challenge["right_code"])
    with verifier.repository.store.db() as db:
        assert db.execute("SELECT attempts FROM governance_challenges").fetchone()[0] == 5
    public = verifier.authority.issue(actor=left, conversation="public", audience="public")
    with pytest.raises(AssistantError):
        verifier.begin(public, right, ["memory/own"])


def test_restore_invalidates_unconsumed_challenges(service):
    verifier, left, right, requests = service
    challenge = verifier.begin(requests[0], right, ["memory/own"])
    verifier.repository.reset_after_restore()
    with pytest.raises(AssistantError):
        verifier.prove(requests[0], challenge["id"], challenge["left_code"])
