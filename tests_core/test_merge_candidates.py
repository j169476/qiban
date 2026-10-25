from itertools import combinations

from companion_core.merge_candidates import neighbors, pair_at, pair_key, plan


def group(number, text=None, version="a"):
    return {"version": version * 64, "data": {"id": f"g-{number:04}", "title": "common",
                                             "content": text or f"topic_{number:04}", "aliases": [], "members": []}}


def test_unranked_audit_cursor_matches_every_unordered_pair():
    for size in range(2, 25):
        assert [pair_at(i, size) for i in range(size * (size - 1) // 2)] == list(combinations(range(size), 2))


def test_lexical_neighbors_find_distant_duplicate_without_all_pair_product():
    groups = [group(i) for i in range(200)]
    groups[0] = group(0, "spectroscopy calibration quartz")
    groups[-1] = group(199, "spectroscopy calibration quartz")
    candidates = neighbors(groups, 4)
    assert (0, 199) in candidates
    assert len(candidates) < 200 * 4
    scheduled = plan(groups, set(), {}, {}, 1000)
    assert scheduled["filtered"] and scheduled["total_pairs"] == 19900
    assert any(pair_key(*p) == pair_key(groups[0], groups[-1]) for p in scheduled["pairs"])
    assert len(scheduled["audit_keys"]) == 4


def test_global_audit_resumes_after_cooldown_and_preserves_unchecked_pairs():
    groups = [group(i) for i in range(40)]
    first = plan(groups, set(), {}, {}, 1000)
    checked = {pair_key(*p) for p in first["pairs"]}
    assert len(checked) == 4
    state = {"audit_cursor": first["audit_cursor"], "audit_at": 1000}
    assert not plan(groups, checked, {}, state, 1001)["pairs"]
    second = plan(groups, checked, {}, state, 1000 + 86400)
    assert len(second["pairs"]) == 4
    assert not checked & {pair_key(*p) for p in second["pairs"]}
    assert len(checked) < first["total_pairs"]  # Screening is not a semantic judgment.


def test_checked_pairs_are_version_bound_and_small_libraries_remain_exhaustive():
    groups = [group(i, "same content") for i in range(30)]
    checked = {pair_key(*p) for p in plan(groups, set(), {}, {}, 1000)["pairs"]}
    later = plan(groups, checked, {}, {"audit_at": 1000}, 1001)
    assert not later["pairs"]
    groups[0]["version"] = "b" * 64
    changed = plan(groups, checked, {}, {"audit_at": 1000}, 1001)
    assert any(groups[0] in p for p in changed["pairs"])
    small = plan(groups[:8], set(), {}, {}, 1000)
    assert not small["filtered"] and len(list(small["pairs"])) == 28
    disabled = plan(groups, set(), {"algorithm_routing": {"merge": {"enabled": False}}}, {}, 1000)
    assert not disabled["filtered"] and len(list(disabled["pairs"])) == 435
