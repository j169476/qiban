"""Desktop stop/lift for one registered conversation, entirely offline.

The registration evidence, account and conversation here are synthetic records of
the shape the desktop app writes; no connector, platform process or model runs.
"""
import uuid

from companion_core.errors import AssistantError
from companion_core.reply_audience import Audience, read
from companion_core.reply_authorization import ReplyAuthorizations
from companion_core.reply_exclusion import ReplyExclusions
from companion_core.reply_policy import Participation, ReplyPolicies
from tests_postgres.test_desktop_reply_quota import (
    OWNER,
    audit_rows,
    authorize,
    denied,
    qq_registered,
    result,
    seeded,
    service_for,
    wechat_registered,
)
from tests_postgres.test_reply_audiences import set_mode


def deny_check(store, scope, stale):
    """True when the stale permission token is refused."""
    try:
        ReplyAuthorizations(store).check(scope, stale)
    except AssistantError:
        return True
    return False


def page(service, platform="qq", account=OWNER):
    return result(service, "reply_quota.scopes", {"platform": platform, "account": account, "after": ""})


def row_of(service, scope, platform="qq", account=OWNER):
    found = page(service, platform, account)
    return next(item for item in found["items"] if item["scope_id"] == scope.id)


def stop(service, scope, account=OWNER, expected=None):
    return result(service, "reply_exclusion.exclude",
                  {"scope_id": scope.id, "account": account, "expected": expected})


def lift(service, scope, account=OWNER, expected=None):
    return result(service, "reply_exclusion.restore",
                  {"scope_id": scope.id, "account": account, "expected": expected})


def snapshot(store, scope):
    return {"authorization": ReplyAuthorizations(store).record(scope),
            "policy": ReplyPolicies(store).get(scope.platform, scope.account_identity),
            "run": store.state("core-run:" + scope.platform),
            "account": store.state("desktop-qq-account")}


def test_offline_stop_and_lift_of_one_registered_conversation(pg_store):
    store = pg_store
    private, group = seeded(store)
    service = service_for(store)
    before = snapshot(store, private)
    initial = row_of(service, private)
    assert initial["excluded"] is False and initial["exclusion_version"] is None
    assert initial["reply_from"] is None and initial["authorized"] is True
    assert initial["status"] == "当前有效：已授权回复该会话"

    stopped = stop(service, private)
    assert stopped["changed"] is True and stopped["excluded"] is True
    assert stopped["excluded_since"] and stopped["reply_from"] is None
    assert stopped["authorized"] is False
    assert stopped["status"].startswith("已排除：已停用该好友")
    assert "解除后只回应解除之后的新消息" in stopped["message"]
    # Trustworthy account-level counts travel with the row so the page can refresh them.
    assert stopped["counts"] == {"registered": 2, "manual": 2, "eligible": 1, "excluded": 1}
    # Nothing else moved: no authorization, no quota policy, no platform lifecycle.
    assert ReplyAuthorizations(store).record(private) == before["authorization"]
    assert ReplyPolicies(store).get("qq", OWNER) == before["policy"]
    assert store.state("core-run:qq") == before["run"]
    assert store.state("desktop-qq-account") == before["account"]
    # The other registered conversation of the same account keeps replying.
    assert row_of(service, group)["authorized"] is True
    assert len(audit_rows(store, "desktop_reply_exclusion")) == 1

    # Repeating the same state is not a new revision.
    again = stop(service, private, expected=stopped["exclusion_version"])
    assert again["changed"] is False and again["exclusion_version"] == stopped["exclusion_version"]
    assert denied(service, "reply_exclusion.restore",
                  {"scope_id": private.id, "account": OWNER, "expected": None}) == "edit_conflict"

    lifted = lift(service, private, expected=stopped["exclusion_version"])
    assert lifted["changed"] is True and lifted["excluded"] is False
    assert lifted["reply_from"] == ReplyExclusions(store).record(private)["not_before"]
    assert lifted["authorized"] is True and lifted["status"] == "当前有效：已授权回复该会话"
    assert lifted["counts"] == {"registered": 2, "manual": 2, "eligible": 2, "excluded": 0}
    assert "只会回应解除之后的新消息" in lifted["message"]
    # The permission is new: the token from before the exclusion cannot come back.
    current = ReplyAuthorizations(store).version(private)
    assert current and current != before["authorization"]["version"]
    assert deny_check(store, private, before["authorization"]["version"])
    assert lift(service, private, expected=lifted["exclusion_version"])["changed"] is False


def test_stop_and_lift_need_a_registered_scope_an_administrator_and_a_clean_restore(pg_store):
    store = pg_store
    private, _group = seeded(store)
    service = service_for(store)

    forged = "reply-scope-v1-" + "0" * 40
    assert denied(service, "reply_exclusion.exclude",
                  {"scope_id": forged, "account": OWNER, "expected": None}) == "reply_quota_unregistered"
    # A scope that exists for another account is a conflict, not a new permission.
    assert denied(service, "reply_exclusion.exclude",
                  {"scope_id": private.id, "account": "99999", "expected": None}) == "edit_conflict"
    assert denied(service, "reply_exclusion.exclude",
                  {"scope_id": private.id, "account": OWNER, "expected": 5}) == "reply_exclusion_invalid"
    assert denied(service, "reply_exclusion.exclude",
                  {"scope_id": "bad id", "account": OWNER, "expected": None}) == "reply_quota_invalid"
    assert denied(service, "reply_exclusion.exclude", {"scope_id": private.id}) == "management_operation"
    assert ReplyExclusions(store).record(private)["excluded"] is False

    # A pending restore blocks the write like the other management changes.
    store.set_state("desktop-last-restore", {"status": "assets_pending"})
    assert denied(service, "reply_exclusion.exclude",
                  {"scope_id": private.id, "account": OWNER, "expected": None}) == "restore_pending"
    store.set_state("desktop-last-restore", {"status": "complete"})

    # One request identifier cannot write twice or be reused for another change.
    request_id = uuid.uuid4().hex
    first = result(service, "reply_exclusion.exclude",
                   {"scope_id": private.id, "account": OWNER, "expected": None}, request_id)
    repeated = result(service, "reply_exclusion.exclude",
                      {"scope_id": private.id, "account": OWNER, "expected": None}, request_id)
    assert repeated == first
    assert ReplyExclusions(store).record(private)["version"] == first["exclusion_version"]
    assert denied(service, "reply_exclusion.restore",
                  {"scope_id": private.id, "account": OWNER, "expected": first["exclusion_version"]},
                  request_id) == "request_id_conflict"

    service.enable_lock("synthetic-password")
    service.lock()
    assert denied(service, "reply_exclusion.restore",
                  {"scope_id": private.id, "account": OWNER,
                   "expected": first["exclusion_version"]}) == "desktop_locked"
    assert ReplyExclusions(store).record(private)["excluded"] is True


def test_lift_never_grants_and_reports_what_still_blocks_a_reply(pg_store):
    store = pg_store
    qq_registered(store, private=("10002", "10004"), groups=())
    revoked = authorize(store, "qq", OWNER, "private", "10002", enabled=False)
    allow_all = authorize(store, "qq", OWNER, "private", "10004", enabled=False)
    service = service_for(store)

    # A revoked conversation stays revoked: lifting the exclusion grants nothing.
    first = stop(service, revoked)
    assert first["status"].startswith("已排除")
    lifted = lift(service, revoked, expected=first["exclusion_version"])
    assert lifted["authorized"] is False and lifted["manual"] is False
    assert lifted["status"].startswith("已撤销")
    assert "仍是已撤销状态" in lifted["message"]
    assert ReplyAuthorizations(store).version(revoked) is None

    # An allow-all category is not an authorization either: it still needs the platform.
    set_mode(store, Audience("qq", OWNER, "private"), True)
    second = stop(service, allow_all)
    assert second["excluded"] is True
    lifted = lift(service, allow_all, expected=second["exclusion_version"])
    assert lifted["authorized"] is False and lifted["allow_all"] is True
    assert "不等于已恢复回复" in lifted["message"]
    assert ReplyAuthorizations(store).version(allow_all) is None


def test_wechat_relationship_still_pending_is_reported_after_a_lift(pg_store):
    store = pg_store
    account_id = wechat_registered(store)
    scope = authorize(store, "wechat", account_id, "private", "wxid_friend")
    rules = ReplyAuthorizations(store)
    record = rules.record(scope)
    with store.db() as db:
        rules.observe_member(scope, False, record["version"], db)
    service = service_for(store)

    stopped = stop(service, scope, account="wxid_synthetic")
    assert stopped["excluded"] is True and stopped["authorized"] is False
    lifted = lift(service, scope, account="wxid_synthetic", expected=stopped["exclusion_version"])
    assert lifted["excluded"] is False and lifted["member_present"] is False
    assert lifted["authorized"] is False
    assert "尚待核验" in lifted["message"]
    assert "关系已离开" in lifted["status"]


def test_stop_and_lift_keep_usage_other_scopes_and_the_account_range(pg_store):
    store = pg_store
    private, group = seeded(store)
    participation = Participation(store)
    for index, outcome in enumerate(("succeeded", "uncertain")):
        ticket = participation.admit(private, "seed-%d" % index, "direct", "run-1")
        participation.reserve(ticket, "run-1", "seed-action-%d" % index)
        participation.settle("seed-action-%d" % index, outcome)
    participation.reserve(participation.admit(private, "seed-reserved", "direct", "run-1"),
                          "run-1", "seed-reserved-action")
    usage = participation.usage(private)[1]
    range_before = {kind: read(store, Audience("qq", OWNER, kind)) for kind in ("private", "group")}
    service = service_for(store)

    stopped = stop(service, private)
    assert stopped["usage"] == usage and stopped["settings"] == {"private_daily_replies": 200}
    lifted = lift(service, private, expected=stopped["exclusion_version"])
    assert lifted["usage"] == usage and participation.usage(private)[1] == usage
    assert lifted["authorized"] is True
    # The account-wide range and the sibling conversation were never touched.
    assert {kind: read(store, Audience("qq", OWNER, kind)) for kind in ("private", "group")} == range_before
    assert row_of(service, group)["authorized"] is True
    # Reply eligibility restarts at the lift, so a message with no usable time is
    # never admitted afterwards, while a newer one is.
    participation = Participation(store)
    assert participation.admit(private, "after-lift", "direct", "run-1") is None
    assert participation.admit(private, "after-lift-timed", "direct", "run-1",
                               message_time=lifted["reply_from"]) is not None
