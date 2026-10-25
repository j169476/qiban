from dataclasses import replace

import pytest

from companion_core.access import (
    AccessGrant,
    AccessService,
    GraphLimits,
    RecordPolicy,
    RequestAuthority,
    RequestContext,
)
from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError


class Repository:
    def __init__(self):
        self.records, self.authorizations = {}, []

    def policy(self, resource):
        return self.records.get(resource)

    def grants(self, grantee):
        return tuple(item for item in self.authorizations if item.grantee == grantee)


@pytest.fixture
def access():
    repository = Repository()
    authority = RequestAuthority(clock=lambda: 1000)
    service = AccessService(repository, authority)
    return repository, authority, service


ALICE = ActorRef("bilibili", "alice")
ALICE_QQ = ActorRef("qq", "alice-qq", "qq-bot")
BOB = ActorRef("bilibili", "bob")


def request(authority, actor=ALICE, audience="private", purpose="reply"):
    return authority.issue(actor=actor, conversation="conversation", audience=audience, purpose=purpose,
                           recipients=(actor,) if audience == "private" else ())


@pytest.mark.parametrize("classification,actor,audience,expected", [
    ("public", BOB, "public", True),
    ("personal", ALICE, "private", True),
    ("personal", ALICE, "public", False),
    ("personal", ALICE, "group", False),
    ("personal", BOB, "private", False),
    ("personal", ALICE_QQ, "private", False),  # even if an identity graph says hard
    ("unclassified", ALICE, "private", False),
    ("secret", ALICE, "private", False),
    ("restricted", ALICE, "private", False),
])
def test_disclosure_matrix(access, classification, actor, audience, expected):
    repo, authority, service = access
    repo.records["phone"] = RecordPolicy("phone", "v1", classification, frozenset((ALICE.key,)))
    assert service.can_read(request(authority, actor, audience), "phone") is expected


def test_subject_mention_does_not_authorize_someone_elses_statement(access):
    repo, authority, service = access
    repo.records["bob-about-alice"] = RecordPolicy("bob-about-alice", "v1", "personal", frozenset((BOB.key,)))
    assert not service.can_read(request(authority), "bob-about-alice")


@pytest.mark.parametrize("basis", ["owner_admin", "account_control"])
def test_cross_account_grant_is_scoped_nontransitive_and_revocable(access, basis):
    repo, authority, service = access
    repo.records["phone"] = RecordPolicy("phone", "v1", "personal", frozenset((ALICE.key,)))
    repo.records["address"] = replace(repo.records["phone"], resource="address")
    grant = AccessGrant("g", ALICE_QQ.key, ALICE.key, frozenset(("phone",)), frozenset(("reply",)),
                        basis, "verified-proof", 1200, 0)
    repo.authorizations.append(grant)
    ctx = request(authority, ALICE_QQ)
    lease = service.lease(ctx, ["phone"])
    assert not service.can_read(ctx, "address")
    assert not service.can_read(request(authority, BOB), "phone")
    assert not service.can_read(request(authority, ALICE_QQ, purpose="exploration"), "phone")
    assert not service.can_read(request(authority, ALICE_QQ, audience="public"), "phone")
    repo.authorizations[0] = replace(grant, revoked=True)
    with pytest.raises(AssistantError):
        service.revalidate(lease)


def test_derived_public_summary_cannot_launder_private_source(access):
    repo, authority, service = access
    repo.records["private"] = RecordPolicy("private", "v1", "personal", frozenset((ALICE.key,)))
    repo.records["summary"] = RecordPolicy("summary", "v1", "public", sources=("private",),
                                            source_versions=(("private", "v1"),))
    assert not service.can_read(request(authority, BOB), "summary")
    lease = service.lease(request(authority), ["summary"])
    repo.records["private"] = replace(repo.records["private"], version="v2", valid=False)
    with pytest.raises(AssistantError):
        service.revalidate(lease)


def test_shared_ancestors_are_read_once_per_check_but_fresh_on_revalidation(access, monkeypatch):
    from collections import Counter

    repo, authority, service = access
    repo.records["source"] = RecordPolicy("source", "v1", "personal", frozenset((ALICE.key,)))
    for key in ("left", "right"):
        repo.records[key] = RecordPolicy(key, "v1", "public", sources=("source",),
                                        source_versions=(("source", "v1"),))
    calls = Counter()
    original = repo.policy

    def counted(key):
        calls[key] += 1
        return original(key)

    monkeypatch.setattr(repo, "policy", counted)
    lease = service.lease(request(authority), ["left", "right", "source"])
    assert calls == {"left": 1, "right": 1, "source": 1}
    repo.records["source"] = replace(repo.records["source"], version="v2", valid=False)
    with pytest.raises(AssistantError):
        service.revalidate(lease)
    assert calls["source"] == 2


def test_cycles_and_excessive_source_graphs_fail_closed(access):
    repo, authority, _ = access
    service = AccessService(repo, authority, limits=GraphLimits(nodes=3))
    repo.records["a"] = RecordPolicy("a", "v1", "public", sources=("b",), source_versions=(("b", "v1"),))
    repo.records["b"] = RecordPolicy("b", "v1", "public", sources=("a",), source_versions=(("a", "v1"),))
    assert not service.can_read(request(authority), "a")
    repo.records["b"] = replace(repo.records["b"], sources=("c",), source_versions=(("c", "v1"),))
    repo.records["c"] = RecordPolicy("c", "v1", "public", sources=("d",), source_versions=(("d", "v1"),))
    repo.records["d"] = RecordPolicy("d", "v1", "public")
    assert not service.can_read(request(authority), "a")


def test_forged_context_expiration_and_restore_epoch(access):
    repo, authority, service = access
    repo.records["public"] = RecordPolicy("public", "v1", "public")
    with pytest.raises(AssistantError):
        service.can_read(RequestContext(ALICE.key, "conversation", "private", frozenset((ALICE.key,)),
                                        "reply", 1200, "", 0, administrator=True), "public")
    ctx = request(authority)
    with pytest.raises(AssistantError):
        service.can_read(replace(ctx, administrator=True), "public")
    authority.epoch = lambda: 1
    with pytest.raises(AssistantError):
        service.can_read(ctx, "public")
    authority.epoch = lambda: 0
    authority.clock = lambda: 1500
    with pytest.raises(AssistantError):
        service.can_read(ctx, "public")


def test_filter_hides_records_before_ranking_and_cannot_leak_titles(access):
    repo, authority, service = access
    repo.records["shown"] = RecordPolicy("shown", "v1", "public")
    repo.records["hidden"] = RecordPolicy("hidden", "v1", "personal", frozenset((BOB.key,)))
    candidates = [{"id": "shown", "title": "public"}, {"id": "hidden", "title": "SECRET"}]
    assert service.filter_before_ranking(request(authority), candidates, lambda item: item["id"]) == candidates[:1]


def test_conversation_scoped_data_stays_in_its_original_audience(access):
    repo, authority, service = access
    repo.records["turn"] = RecordPolicy("turn", "v1", "conversation", conversation="conversation", audience="group")
    assert service.can_read(request(authority, audience="group"), "turn")
    assert not service.can_read(request(authority), "turn")
