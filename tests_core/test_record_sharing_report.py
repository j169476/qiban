"""Independent diagnostic parity on a shared source DAG."""
from types import SimpleNamespace

import pytest

from companion_core.access import AccessService, GraphLimits, RecordPolicy, RequestAuthority
from companion_core.contracts import ActorRef
from companion_core.record_sharing import RecordSharing


@pytest.mark.parametrize('depth,expected', [(3, False), (4, True)])
def test_shared_source_longest_path_matches_core_read_decision(depth, expected):
    nodes = {}
    for key, children in [('leaf', ()), ('a', ('leaf',)), ('b', ('a',)), ('root', ('a', 'b'))]:
        nodes[key] = RecordPolicy(key, 'v1', 'public', frozenset(), sources=children,
                                  source_versions=tuple((child, 'v1') for child in children))
    repository = SimpleNamespace(policy=nodes.get, clock=lambda: 0, grants=lambda actor: ())
    authority = RequestAuthority()
    actor = ActorRef('qq', 'synthetic', 'sharing-report')
    access = AccessService(repository, authority, limits=GraphLimits(depth=depth))
    sharing = object.__new__(RecordSharing)
    sharing.repository, sharing.access = repository, access
    report = sharing._report('root')
    request = authority.issue(actor=actor, conversation='synthetic', audience='public', purpose='reply')
    assert access.can_read(request, 'root') is expected
    assert report['ok'] is expected
    assert report['depth'] == 4
    if not expected:
        assert any(issue['reason'] == 'depth' for issue in report['issues'])
