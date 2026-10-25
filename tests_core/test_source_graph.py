from dataclasses import replace

import pytest

from companion_core.access import AccessService, GraphLimits, RecordPolicy
from companion_core.errors import AssistantError
from tests_core.test_access_matrix import access, request  # noqa: F401


def chain(repo, size):
    for index in range(size):
        sources = (str(index - 1),) if index else ()
        repo.records[str(index)] = RecordPolicy(str(index), "v1", "public", sources=sources,
                                                source_versions=tuple((key, "v1") for key in sources))


def test_deep_graph_is_iterative_complete_and_direct_pins_stay_small(access):  # noqa: F811
    repo, authority, service = access
    chain(repo, 3000)
    lease = service.lease(request(authority), ["2999"])
    assert lease.roots == ("2999",) and len(lease.versions) == 3000
    assert service.readable_snapshot(lease.request, repo.records, ()) == set(repo.records)
    repo.records["0"] = replace(repo.records["0"], valid=False)
    with pytest.raises(AssistantError):
        service.revalidate(lease)
    assert not service.readable_snapshot(lease.request, repo.records, ())


@pytest.mark.parametrize("limits,size,allowed", [
    (GraphLimits(nodes=4), 4, True), (GraphLimits(nodes=4), 5, False),
    (GraphLimits(depth=4), 4, True), (GraphLimits(depth=4), 5, False),
    (GraphLimits(edges=3), 4, True), (GraphLimits(edges=3), 5, False),
])
def test_catalog_and_disclosure_have_identical_graph_limits(access, limits, size, allowed):  # noqa: F811
    repo, authority, _ = access
    chain(repo, size)
    service = AccessService(repo, authority, limits=limits)
    ctx = request(authority)
    assert service.can_read(ctx, str(size - 1)) == allowed
    assert (str(size - 1) in service.readable_snapshot(ctx, repo.records, ())) == allowed


def test_time_budget_is_not_extended_by_repository_reads(access, monkeypatch):  # noqa: F811
    repo, authority, service = access
    chain(repo, 2)
    ticks = iter([0, 0, 3, 3, 3])
    monkeypatch.setattr("companion_core.access.time.monotonic", lambda: next(ticks, 3))
    assert not service.can_read(request(authority), "1")


def test_shared_paths_use_unique_nodes_and_edges_and_enforce_longest_depth(access):  # noqa: F811
    repo, authority, _ = access
    chain(repo, 4)
    repo.records["root"] = RecordPolicy("root", "v1", "public", sources=("0", "3"),
                                         source_versions=(("0", "v1"), ("3", "v1")))
    for depth, allowed in ((4, False), (5, True)):
        service = AccessService(repo, authority, limits=GraphLimits(nodes=5, edges=5, depth=depth))
        ctx = request(authority)
        assert service.can_read(ctx, "root") == allowed
        assert ("root" in service.readable_snapshot(ctx, repo.records, ())) == allowed


def test_cycle_missing_and_duplicate_pins_fail_closed_in_catalog(access):  # noqa: F811
    repo, authority, service = access
    chain(repo, 4)
    repo.records["0"] = replace(repo.records["0"], sources=("3",), source_versions=(("3", "v1"),))
    ctx = request(authority)
    assert not service.readable_snapshot(ctx, repo.records, ())
    assert not service.can_read(ctx, "3")
    repo.records["0"] = replace(repo.records["0"], sources=("missing",), source_versions=(("missing", "v1"),))
    assert not service.readable_snapshot(ctx, repo.records, ())
    repo.records["0"] = replace(repo.records["0"], sources=(), source_versions=(("extra", "v1"),))
    assert not service.can_read(ctx, "3")
