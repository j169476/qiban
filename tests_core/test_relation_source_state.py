"""Whole-closure source state for claim adjudication: the digest must be honest.

Three ways a stale preview could wrongly commit, each pinned here on a pure
metadata fixture (no database, platform or model):

* a read timestamp inside the digest, so an ordinary preview is refused as the
  clock advances;
* two different rotations of one source producing identical issue wording;
* an indirect (transitive) change a direct-only report never walks.

The report wording and the bound state are separate: the wording is for people,
the digest has to cover every visited node's exact version, validity-at-check-time,
expiry-at-check-time and pins.
"""
from dataclasses import replace
from types import SimpleNamespace

from companion_core import source_report
from companion_core.access import GraphLimits, RecordPolicy

LIMITS = GraphLimits()


def policy(resource, version, *, sources=(), pins=(), valid=True, expires_at=None,
           classification="personal"):
    return RecordPolicy(resource, version, classification, frozenset(("actor-a",)), "", "private",
                        tuple(sources), valid, expires_at, tuple(pins))


def repository(policies, clock=lambda: 0.0):
    return SimpleNamespace(policy=policies.get, clock=clock, provenance_target=lambda _: None,
                           grants=lambda actor: ())


def root(pins=("event/e", "ev1")):
    return policy("relation/c", "p1", sources=(pins[0],), pins=((pins[0], pins[1]),))


def two_nodes(event_version="ev1"):
    return {"relation/c": root(), "event/e": policy("event/e", event_version)}


def check(policies, **kwargs):
    return source_report.closure(repository(policies), LIMITS, "relation/c", **kwargs)


def test_an_advancing_clock_alone_never_changes_the_check():
    policies = two_nodes()
    first = check(policies)
    later = source_report.closure(repository(policies, clock=lambda: 9e9), LIMITS, "relation/c")
    assert first["report"]["ok"] is True
    # No read timestamp may enter the report: a clock tick is not a change.
    assert "checked_at" not in first["report"]
    assert first["report"] == later["report"]
    assert source_report.digest(first) == source_report.digest(later)


def test_a_second_rotation_cannot_look_like_the_first():
    policies = two_nodes("ev2")
    rotated = check(policies)
    policies["event/e"] = policy("event/e", "ev3")
    rotated_again = check(policies)
    # The wording is identical; only the bound state tells the two apart.
    assert rotated["report"] == rotated_again["report"]
    assert [issue["reason"] for issue in rotated["report"]["issues"]] == ["source_changed"]
    assert source_report.digest(rotated) != source_report.digest(rotated_again)


def test_a_metadata_edit_without_a_version_bump_is_bound():
    policies = two_nodes()
    policies["event/e"] = policy("event/e", "ev1")
    service = repository(policies, clock=lambda: 2000000000.0)
    edited = dict(policies)
    edited["event/e"] = policy("event/e", "ev1", expires_at=2000000200.0)
    first = source_report.closure(service, LIMITS, "relation/c")
    # Same policy version, only the stored expiry moved: still a different state.
    edited["event/e"] = policy("event/e", "ev1", expires_at=2000000400.0)
    second = source_report.closure(repository(edited, clock=lambda: 2000000000.0),
                                   LIMITS, "relation/c")
    assert first["report"] == second["report"]
    assert source_report.digest(first) != source_report.digest(second)
    # Crossing the stored expiry between a preview and its commit changes it too.
    policies["event/e"] = policy("event/e", "ev1", expires_at=2000000200.0)
    before_expiry = source_report.closure(repository(policies, clock=lambda: 2000000000.0),
                                          LIMITS, "relation/c")
    after_expiry = source_report.closure(repository(policies, clock=lambda: 2000000300.0),
                                         LIMITS, "relation/c")
    assert before_expiry["report"]["ok"] is True
    assert after_expiry["report"]["ok"] is False
    assert source_report.digest(before_expiry) != source_report.digest(after_expiry)


def test_a_missing_source_state_is_bound_as_well():
    policies = two_nodes()
    healthy = check(policies)
    del policies["event/e"]
    missing = check(policies)
    policies["event/e"] = policy("event/e", "ev1")
    restored = check(policies)
    assert missing["report"]["ok"] is False
    assert any(issue["reason"] == "source_missing" for issue in missing["report"]["issues"])
    assert source_report.digest(healthy) != source_report.digest(missing)
    assert source_report.digest(healthy) == source_report.digest(restored)


def indirect(child):
    return {"relation/c": root(), "event/e": policy("event/e", "ev1", sources=("knowledge/p",),
                                                    pins=(("knowledge/p", "kp1"),)),
            "knowledge/p": child}


def test_an_indirect_source_rotation_is_walked_and_bound():
    policies = indirect(policy("knowledge/p", "kp1", classification="public"))
    healthy = check(policies)
    assert healthy["report"]["ok"] is True and healthy["report"]["nodes"] == 3
    policies["knowledge/p"] = policy("knowledge/p", "kp2", classification="public")
    rotated = check(policies)
    assert rotated["report"]["ok"] is False
    assert any(issue["resource"] == "knowledge/p" and issue["reason"] == "source_changed"
               for issue in rotated["report"]["issues"])
    assert source_report.digest(healthy) != source_report.digest(rotated)


def test_an_indirect_invalid_or_expired_or_quarantined_source_is_not_healthy():
    healthy = check(indirect(policy("knowledge/p", "kp1", classification="public")))
    for child, reason in ((policy("knowledge/p", "kp1", classification="public", valid=False),
                           "revoked"),
                          (policy("knowledge/p", "kp1", classification="public", expires_at=5.0),
                           "expired"),
                          (policy("knowledge/p", "kp1", classification="secret"), "quarantined")):
        broken = source_report.closure(repository(indirect(child), clock=lambda: 10.0), LIMITS,
                                       "relation/c")
        assert broken["report"]["ok"] is False, reason
        assert any(issue["reason"] == reason for issue in broken["report"]["issues"]), reason
        assert source_report.digest(healthy) != source_report.digest(broken)


def test_a_broken_indirect_graph_is_reported_not_called_healthy():
    cyclic = indirect(policy("knowledge/p", "kp1", sources=("relation/c",),
                             pins=(("relation/c", "p1"),)))
    cycle = check(cyclic)
    assert cycle["report"]["ok"] is False
    assert any(issue["reason"] == "cycle" for issue in cycle["report"]["issues"])

    missing = indirect(policy("knowledge/p", "kp1", sources=("knowledge/absent",),
                              pins=(("knowledge/absent", "x1"),)))
    absent = check(missing)
    assert absent["report"]["ok"] is False
    assert any(issue["reason"] == "source_missing" for issue in absent["report"]["issues"])

    chain = {"relation/c": policy("relation/c", "p1", sources=("knowledge/deep-0",),
                                  pins=(("knowledge/deep-0", "v"),)),
             "knowledge/deep-0": policy("knowledge/deep-0", "v")}
    deep = source_report.closure(repository(chain), GraphLimits(depth=1), "relation/c")
    assert deep["report"]["ok"] is False
    assert any(issue["reason"] == "depth" for issue in deep["report"]["issues"])


def test_the_root_override_describes_the_claim_only_and_sources_stay_current():
    revoked = {"relation/c": policy("relation/c", "p1", valid=False, sources=("event/e",),
                                    pins=(("event/e", "ev1"),)),
               "event/e": policy("event/e", "ev1")}
    current = check(revoked)
    projected = check(revoked, root=replace(revoked["relation/c"], valid=True))
    # Revoking/restoring is a decision about the claim itself ...
    assert current["report"]["ok"] is False and projected["report"]["ok"] is True
    assert source_report.digest(current) != source_report.digest(projected)
    # ... and never hides a source that really changed.
    revoked["event/e"] = policy("event/e", "ev2")
    still_broken = check(revoked, root=replace(revoked["relation/c"], valid=True))
    assert still_broken["report"]["ok"] is False
    assert any(issue["reason"] == "source_changed" for issue in still_broken["report"]["issues"])
