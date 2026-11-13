"""Reply quota editing with saved, previously verified registration only.

These tests use the disposable PostgreSQL schema fixture and never contact a
connector, platform process or model: every account, conversation and usage
number below is synthetic.
"""
import hashlib
import secrets
import uuid

from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_core.reply_policy import BOUNDS, DEFAULTS, Participation, ReplyPolicies
from qiban.desktop_management import DesktopManagement, provision_owner

INSTALLATION = "installation-quota"
SID = "S-1-5-21-quota-owner"
OWNER = "10001"


def call(service, op, args=None, request_id=None):
    return service.dispatch({"action": "management", "operation": op, "arguments": args or {},
                             "request_id": request_id or uuid.uuid4().hex})


def result(service, op, args=None, request_id=None):
    reply = call(service, op, args, request_id)
    assert reply["ok"], reply
    return reply["result"]


def denied(service, op, args=None, request_id=None):
    reply = call(service, op, args, request_id)
    assert not reply["ok"], reply
    return reply["error"]


def qq_registered(store, uin=OWNER, uid="u_synthetic", private=("10002",), groups=("10003",),
                  *, selected=True, enroll=True):
    """The durable shape desktop_qq_targets.save writes, without any connector."""
    from companion_qq.module import enrollment_intent

    current = store.state("desktop-qq-account", {"revision": 0, "selected": None, "configurations": {}})
    configurations = dict(current.get("configurations") or {})
    configurations[uin] = {"uid": uid, "private_targets": list(private), "groups": list(groups)}
    store.set_state("desktop-qq-account", {
        "revision": current.get("revision", 0) + 1,
        "selected": {"uin": uin, "uid": uid, "label": "合成 QQ 账号"} if selected else current.get("selected"),
        "configurations": configurations})
    if enroll:
        intent = enrollment_intent({"version": 2, "transport": "native", "account": uin, "uid": uid,
                                    "private_targets": list(private), "groups": list(groups),
                                    "runtime": "C:/synthetic/runtime", "data": "C:/synthetic/data"})
        store.set_state("qq-enrollment:" + uin, {
            "intent": intent,
            "identity": {"account": uin, "account_label": uin,
                         "private_targets": list(private), "groups": list(groups)}})
    return {"uin": uin, "uid": uid}


def wechat_registered(store, installation=INSTALLATION, login="wxid_synthetic",
                      targets=(("owner_main", "private", "wxid_friend"), ("family", "group", "999@chatroom")),
                      *, legacy=False):
    """Installation-owned profile plus enrollment snapshot, as enrollment writes them."""
    from companion_core.contracts import stable_key
    from qiban.desktop_wechat import SETUP, enrollment_key
    from qiban.desktop_wechat_targets import identity

    account_id = identity(login)
    scoped = installation if legacy else stable_key("desktop-wechat-profile", installation, login)
    setup_key = SETUP if legacy else scoped
    store.set_state(stable_key("desktop-wechat-selection", installation),
                    {"revision": 1, "selected": login, "accounts": [login]})
    store.set_state(setup_key, {"directory": "C:/synthetic/wechat", "identity": account_id, "revision": 1})
    enrollment = {"schema_version": 2, "epoch": secrets.token_hex(16), "account_id": account_id,
                  "self_username": login, "since": 1,
                  "account_directory_id": hashlib.sha256(b"synthetic-wechat-directory").hexdigest(),
                  "targets": {alias: {"kind": kind, "username": user} for alias, kind, user in targets},
                  "target_since": {alias: 1 for alias, _kind, _user in targets}}
    store.set_state(enrollment_key(scoped), {
        "enrollment": enrollment,
        "checkpoint": {"schema_version": 1, "epoch": enrollment["epoch"], "sources": {}}})
    return account_id


def authorize(store, platform, account, kind, target, *, enabled=True):
    scope = ReplyScope(platform, account, kind, target)
    ReplyAuthorizations(store).update(scope, enabled=enabled, expected_version=None, actor="synthetic")
    return scope


def seeded(store):
    qq_registered(store)
    return (authorize(store, "qq", OWNER, "private", "10002"),
            authorize(store, "qq", OWNER, "group", "10003"))


def service_for(store):
    provision_owner(store, INSTALLATION, SID)
    return DesktopManagement(store, INSTALLATION, SID)


def snapshot(store, scope):
    record = ReplyAuthorizations(store).record(scope)
    return {"record": record, "run": store.state("core-run:qq"),
            "account": store.state("desktop-qq-account")}


def audit_rows(store, operation):
    with store.db() as db:
        return db.execute("SELECT resource,before_version,after_version FROM governance_audit "
                          "WHERE operation=?", (operation,)).fetchall()


# --- offline reading and writing ------------------------------------------------


def test_saved_qq_account_reads_and_writes_defaults_without_a_connector(pg_store):
    store = pg_store
    private, group = seeded(store)
    service = service_for(store)

    summary = result(service, "reply_quota.summary", {"after": ""})
    assert [row["account"] for row in summary["accounts"]] == [OWNER]
    account = summary["accounts"][0]
    assert account["selected"] is True and account["registered"] == 2
    assert (account["manual"], account["eligible"]) == (2, 2)
    assert account["allow_all"] == {"private": False, "group": False}
    assert account["platform_running"] is False and account["version"] is None
    assert summary["total"] == 1 and summary["next"] is None
    assert account["settings"] == DEFAULTS
    assert {key: tuple(value) for key, value in summary["bounds"].items()} == BOUNDS
    assert summary["billing"] is False and summary["quota_scope"] == "contact_calendar_day"
    assert summary["timezone"] and summary["day"]

    page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
    assert page["total"] == 2 and page["next"] is None
    kinds = {row["kind"] for row in page["items"]}
    assert kinds == {"private", "group"}
    contact = next(row for row in page["items"] if row["scope_id"] == private.id)
    assert contact["authorized"] is True and contact["inherited"] is True
    assert contact["usage"] == {"confirmed": 0, "reserved": 0, "uncertain": 0}
    assert contact["fields"] == ["private_daily_replies"]
    stage = next(row for row in page["items"] if row["scope_id"] == group.id)
    assert stage["fields"] == ["group_session_replies", "group_session_decisions", "group_session_minutes"]
    assert stage["window"]["current"] is False and stage["window"]["note"] == "尚无群参与阶段"

    saved = result(service, "reply_quota.account_save",
                   {"platform": "qq", "account": OWNER, "expected": None,
                    "changes": {"private_daily_replies": 7, "group_session_replies": 3}})
    assert saved["changed"] is True and isinstance(saved["version"], str)
    assert saved["settings"] == {**DEFAULTS, "private_daily_replies": 7, "group_session_replies": 3}
    assert ReplyPolicies(store).get("qq", OWNER)["settings"]["private_daily_replies"] == 7
    assert len(audit_rows(store, "desktop_reply_quota_account")) == 1

    # Saving the same numbers again is not a second version.
    again = result(service, "reply_quota.account_save",
                   {"platform": "qq", "account": OWNER, "expected": saved["version"],
                    "changes": {"private_daily_replies": 7}})
    assert again["changed"] is False and again["version"] == saved["version"]


def test_first_enrollment_shape_of_wechat_is_editable_offline(pg_store):
    store = pg_store
    account_id = wechat_registered(store, legacy=True)
    authorize(store, "wechat", account_id, "private", "wxid_friend")
    authorize(store, "wechat", account_id, "group", "999@chatroom")
    service = service_for(store)

    summary = result(service, "reply_quota.summary", {"after": ""})
    assert [(row["platform"], row["account"]) for row in summary["accounts"]] == [("wechat", "wxid_synthetic")]
    assert summary["accounts"][0]["label"] == "wxid_synthetic"
    saved = result(service, "reply_quota.account_save",
                   {"platform": "wechat", "account": "wxid_synthetic", "expected": None,
                    "changes": {"private_daily_replies": 40}})
    assert saved["settings"]["private_daily_replies"] == 40
    page = result(service, "reply_quota.scopes",
                  {"platform": "wechat", "account": "wxid_synthetic", "after": ""})
    assert page["total"] == 2
    private_rows = [row for row in page["items"] if row["kind"] == "private"]
    assert [row["settings"] for row in private_rows] == [{"private_daily_replies": 40}]


def test_second_wechat_account_in_saved_selection_is_editable(pg_store):
    from companion_core.contracts import stable_key

    store = pg_store
    account_id = wechat_registered(store)
    store.set_state(stable_key("desktop-wechat-selection", INSTALLATION),
                    {"revision": 2, "selected": "wxid_other", "accounts": ["wxid_other", "wxid_synthetic"]})
    authorize(store, "wechat", account_id, "private", "wxid_friend")
    service = service_for(store)

    summary = result(service, "reply_quota.summary", {"after": ""})
    # The selection points at another login, but the saved, verified profile stays manageable.
    assert [(row["account"], row["selected"]) for row in summary["accounts"]] == [("wxid_synthetic", False)]
    saved = result(service, "reply_quota.account_save",
                   {"platform": "wechat", "account": "wxid_synthetic", "expected": None,
                    "changes": {"private_daily_replies": 12}})
    assert saved["settings"]["private_daily_replies"] == 12


def test_historical_second_qq_account_is_editable_and_cross_account_scope_is_refused(pg_store):
    store = pg_store
    second = "10009"
    qq_registered(store)
    qq_registered(store, uin=second, uid="u_second", private=("10010",), groups=(), selected=False)
    other = authorize(store, "qq", second, "private", "10010")
    service = service_for(store)

    summary = result(service, "reply_quota.summary", {"after": ""})
    assert [(row["account"], row["selected"]) for row in summary["accounts"]] == [(OWNER, True), (second, False)]
    # A saved but unselected account keeps its own defaults.
    assert result(service, "reply_quota.account_save",
                  {"platform": "qq", "account": second, "expected": None,
                   "changes": {"private_daily_replies": 4}})["settings"]["private_daily_replies"] == 4
    # The same scope cannot be written through another account's page.
    page = result(service, "reply_quota.scopes", {"platform": "qq", "account": second, "after": ""})
    row = page["items"][0]
    assert denied(service, "reply_quota.scope_save",
                  {"scope_id": row["scope_id"], "account": OWNER, "expected": row["version"],
                   "changes": {"private_daily_replies": 1}}) == "edit_conflict"
    assert ReplyPolicies(store).effective(other)["inherited"] is True
    assert result(service, "reply_quota.scope_save",
                  {"scope_id": other.id, "account": second, "expected": row["version"],
                   "changes": {"private_daily_replies": 1}})["settings"]["private_daily_replies"] == 1


def test_account_with_zero_registered_targets_still_edits_defaults(pg_store):
    store = pg_store
    qq_registered(store, private=(), groups=())
    service = service_for(store)

    summary = result(service, "reply_quota.summary", {"after": ""})
    assert summary["accounts"][0]["registered"] == 0
    page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
    assert page["items"] == [] and page["total"] == 0 and page["next"] is None
    saved = result(service, "reply_quota.account_save",
                   {"platform": "qq", "account": OWNER, "expected": None,
                    "changes": {"group_session_decisions": 5}})
    assert saved["settings"]["group_session_decisions"] == 5


# --- refusals -------------------------------------------------------------------


def test_unknown_forged_and_unverified_accounts_are_refused(pg_store):
    store = pg_store
    service = service_for(store)
    assert result(service, "reply_quota.summary", {"after": ""})["accounts"] == []
    for account in ("99999", "abc"):
        assert denied(service, "reply_quota.account_save",
                      {"platform": "qq", "account": account, "expected": None,
                       "changes": {"private_daily_replies": 2}}) == "reply_quota_unregistered"
    assert denied(service, "reply_quota.account_save",
                  {"platform": "qq", "account": "", "expected": None,
                   "changes": {"private_daily_replies": 2}}) == "reply_quota_invalid"
    assert denied(service, "reply_quota.account_save",
                  {"platform": "telegram", "account": OWNER, "expected": None,
                   "changes": {"private_daily_replies": 2}}) == "reply_quota_invalid"

    # A saved configuration without an enrollment record is not evidence.
    qq_registered(store, enroll=False)
    assert result(service, "reply_quota.summary", {"after": ""})["accounts"] == []
    assert denied(service, "reply_quota.account_save",
                  {"platform": "qq", "account": OWNER, "expected": None,
                   "changes": {"private_daily_replies": 2}}) == "reply_quota_unregistered"

    # A tampered enrollment intent, or a target list the probe never verified, stays untrusted.
    qq_registered(store)
    store.set_state("qq-enrollment:" + OWNER, {"intent": "0" * 64,
                                               "identity": {"account": OWNER, "private_targets": ["10002"],
                                                            "groups": ["10003"]}})
    assert result(service, "reply_quota.summary", {"after": ""})["accounts"] == []
    qq_registered(store)
    enrollment = store.state("qq-enrollment:" + OWNER)
    store.set_state("qq-enrollment:" + OWNER, {**enrollment, "identity": {
        **enrollment["identity"], "private_targets": ["10002", "10099"]}})
    assert result(service, "reply_quota.summary", {"after": ""})["accounts"] == []


def test_scope_identifier_must_exist_and_belong_to_a_verified_account(pg_store):
    store = pg_store
    seeded(store)
    service = service_for(store)
    page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
    version = page["items"][0]["version"]
    forged = secrets.token_hex(16)
    for scope_id in (forged, page["items"][0]["scope_id"] + "x"):
        assert denied(service, "reply_quota.scope_save",
                      {"scope_id": scope_id, "account": OWNER, "expected": version,
                       "changes": {"private_daily_replies": 3}}) == "reply_quota_unregistered"
    assert denied(service, "reply_quota.scope_save",
                  {"scope_id": "not a scope", "account": OWNER, "expected": version,
                   "changes": {"private_daily_replies": 3}}) == "reply_quota_invalid"
    # Removing the enrollment evidence revokes quota editing for the same scope.
    store.set_state("qq-enrollment:" + OWNER, {"intent": "0" * 64, "identity": {"account": OWNER}})
    assert denied(service, "reply_quota.scope_save",
                  {"scope_id": page["items"][0]["scope_id"], "account": OWNER, "expected": version,
                   "changes": {"private_daily_replies": 3}}) == "reply_quota_unregistered"


def test_locked_management_refuses_reads_and_writes(pg_store):
    store = pg_store
    private, _group = seeded(store)
    service = service_for(store)
    service.enable_lock("synthetic-password")
    service.lock()
    assert denied(service, "reply_quota.summary", {"after": ""}) == "desktop_locked"
    assert denied(service, "reply_quota.account_save",
                  {"platform": "qq", "account": OWNER, "expected": None,
                   "changes": {"private_daily_replies": 9}}) == "desktop_locked"
    assert ReplyPolicies(store).get("qq", OWNER)["version"] is None
    assert ReplyAuthorizations(store).record(private)["enabled"] is True


def test_invalid_quota_values_are_rejected_without_writing(pg_store):
    store = pg_store
    seeded(store)
    service = service_for(store)
    for changes in ({}, {"private_daily_replies": True}, {"private_daily_replies": 1.5},
                    {"private_daily_replies": "5"}, {"unknown": 3},
                    {"private_daily_replies": BOUNDS["private_daily_replies"][1] + 1},
                    {"group_session_minutes": BOUNDS["group_session_minutes"][0] - 1}):
        reply = call(service, "reply_quota.account_save",
                     {"platform": "qq", "account": OWNER, "expected": None, "changes": changes})
        assert reply["error"] == "reply_quota_invalid", changes
    assert ReplyPolicies(store).get("qq", OWNER)["version"] is None
    assert denied(service, "reply_quota.account_save",
                  {"platform": "qq", "account": OWNER, "expected": 5,
                   "changes": {"private_daily_replies": 5}}) == "reply_quota_invalid"


def test_missing_required_arguments_are_refused_by_the_registry(pg_store):
    store = pg_store
    seeded(store)
    service = service_for(store)
    assert denied(service, "reply_quota.scope_save", {"scope_id": "x", "account": OWNER}) == "management_operation"
    assert denied(service, "reply_quota.summary", {"account": OWNER}) == "management_operation"
    assert denied(service, "reply_quota.summary", {}) == "management_operation"
    assert denied(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER}) == "management_operation"


# --- authorization, lifecycle and usage are unchanged ---------------------------


def test_quota_writes_leave_authorization_lifecycle_and_registration_untouched(pg_store):
    store = pg_store
    private, group = seeded(store)
    store.set_state("core-run:qq", {"owner": "synthetic", "generation": "run-1", "status": "active"})
    service = service_for(store)
    before = {scope.id: snapshot(store, scope) for scope in (private, group)}

    def page_rows():
        page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
        return {row["scope_id"]: row for row in page["items"]}

    rows = page_rows()
    result(service, "reply_quota.account_save",
           {"platform": "qq", "account": OWNER, "expected": None,
            "changes": {"private_daily_replies": 50, "group_session_replies": 4}})
    # Changing the account default changes every inheriting scope version too.
    assert denied(service, "reply_quota.scope_save",
                  {"scope_id": group.id, "account": OWNER, "expected": rows[group.id]["version"],
                   "changes": {"group_session_minutes": 6}}) == "edit_conflict"
    result(service, "reply_quota.scope_save",
           {"scope_id": group.id, "account": OWNER, "expected": page_rows()[group.id]["version"],
            "changes": {"group_session_minutes": 6}})
    result(service, "reply_quota.scope_reset",
           {"scope_id": group.id, "account": OWNER, "expected": page_rows()[group.id]["version"]})
    assert page_rows()[group.id]["inherited"] is True
    for scope in (private, group):
        assert snapshot(store, scope) == before[scope.id]
    # No authorization is created for an unregistered target.
    assert not any(record["scope"]["target_id"] == "10099"
                   for record in ReplyAuthorizations(store).records())


def test_defaults_are_editable_while_the_platform_keeps_running(pg_store):
    store = pg_store
    seeded(store)
    run = {"owner": "synthetic", "generation": "run-1", "status": "active"}
    store.set_state("core-run:qq", run)
    service = service_for(store)
    saved = result(service, "reply_quota.account_save",
                   {"platform": "qq", "account": OWNER, "expected": None,
                    "changes": {"private_daily_replies": 25}})
    assert saved["settings"]["private_daily_replies"] == 25
    assert store.state("core-run:qq") == run


def test_private_usage_is_preserved_and_never_reset_by_a_quota_edit(pg_store):
    store = pg_store
    private, _group = seeded(store)
    participation = Participation(store)
    for index, outcome in enumerate(("succeeded", "uncertain")):
        ticket = participation.admit(private, "seed-%d" % index, "direct", "run-1")
        participation.reserve(ticket, "run-1", "seed-action-%d" % index)
        participation.settle("seed-action-%d" % index, outcome)
    reserved = participation.admit(private, "seed-reserved", "direct", "run-1")
    participation.reserve(reserved, "run-1", "seed-reserved-action")
    usage = participation.usage(private)[1]
    assert usage == {"confirmed": 1, "reserved": 1, "uncertain": 1}
    service = service_for(store)
    page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
    row = next(row for row in page["items"] if row["scope_id"] == private.id)
    assert row["usage"] == usage and row["used"] == 3

    saved = result(service, "reply_quota.account_save",
                   {"platform": "qq", "account": OWNER, "expected": None,
                    "changes": {"private_daily_replies": 3}})
    assert saved["settings"]["private_daily_replies"] == 3
    assert participation.usage(private)[1] == usage
    # Three replies are already counted, so the new cap blocks the next one.
    assert participation.admit(private, "after-edit", "direct", "run-1") is None
    raised = result(service, "reply_quota.account_save",
                    {"platform": "qq", "account": OWNER, "expected": saved["version"],
                     "changes": {"private_daily_replies": 30}})
    assert raised["settings"]["private_daily_replies"] == 30
    assert participation.usage(private)[1] == usage
    assert participation.admit(private, "after-raise", "direct", "run-1") is not None


def test_group_stage_usage_is_shown_and_a_lower_cap_closes_it_at_once(pg_store):
    store = pg_store
    qq_registered(store, groups=("10003",))
    group = authorize(store, "qq", OWNER, "group", "10003")
    store.set_state("core-run:qq", {"owner": "synthetic", "generation": "run-1", "status": "active"})
    participation = Participation(store)
    for index in range(3):
        ticket = participation.admit(group, "stage-%d" % index, "mention" if index == 0 else "none", "run-1")
        participation.reserve(ticket, "run-1", "stage-action-%d" % index)
        participation.settle("stage-action-%d" % index, "succeeded")
    service = service_for(store)
    page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
    row = next(row for row in page["items"] if row["scope_id"] == group.id)
    assert row["window"]["current"] is True and row["window"]["used"] == 3
    assert row["window"]["reply_limit"] == DEFAULTS["group_session_replies"]
    assert row["window"]["decisions"] == 3

    # The page always submits the whole applicable group form, not just the change.
    saved = result(service, "reply_quota.scope_save",
                   {"scope_id": group.id, "account": OWNER, "expected": row["version"],
                    "changes": {"group_session_replies": 2, "group_session_decisions": 20,
                                "group_session_minutes": 10}})
    assert saved["settings"]["group_session_replies"] == 2
    window = store.state(Participation.window_key(group))
    assert window["status"] == "closed" and window["reason"] == "quota_changed"
    assert (window["confirmed"], window["reserved"], window["uncertain"]) == (3, 0, 0)
    page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
    row = next(row for row in page["items"] if row["scope_id"] == group.id)
    assert row["window"]["current"] is False and "额度调整" in row["window"]["note"]
    assert row["authorized"] is True and row["settings"]["group_session_replies"] == 2

    # Raising the cap later must not reopen the finished stage.
    raised = result(service, "reply_quota.scope_save",
                    {"scope_id": group.id, "account": OWNER, "expected": saved["version"],
                     "changes": {"group_session_replies": 9}})
    assert raised["settings"]["group_session_replies"] == 9
    still = store.state(Participation.window_key(group))
    assert still["id"] == window["id"] and still["status"] == "closed" and still["confirmed"] == 3


def test_group_stage_of_a_finished_run_is_marked_historical(pg_store):
    store = pg_store
    qq_registered(store, groups=("10003",))
    group = authorize(store, "qq", OWNER, "group", "10003")
    store.set_state("core-run:qq", {"owner": "synthetic", "generation": "run-1", "status": "active"})
    participation = Participation(store)
    ticket = participation.admit(group, "stage", "mention", "run-1")
    participation.reserve(ticket, "run-1", "stage-action")
    participation.settle("stage-action", "succeeded")
    service = service_for(store)

    def stage():
        page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
        return next(row for row in page["items"] if row["scope_id"] == group.id)["window"]

    assert stage()["current"] is True
    store.set_state("core-run:qq", {"owner": "synthetic", "generation": "run-2", "status": "active"})
    assert stage()["current"] is False and "运行" in stage()["note"]
    store.set_state("core-run:qq", {"owner": "synthetic", "generation": "run-2", "status": "paused"})
    assert stage()["current"] is False
    # Revoking the conversation authorization also makes the stage historical.
    store.set_state("core-run:qq", {"owner": "synthetic", "generation": "run-1", "status": "active"})
    ReplyAuthorizations(store).update(group, enabled=False,
                                      expected_version=ReplyAuthorizations(store).record(group)["version"],
                                      actor="synthetic")
    assert stage()["current"] is False and "授权" in stage()["note"]


# --- overrides, conflicts and paging -------------------------------------------


def test_override_inheritance_restore_and_stale_versions(pg_store):
    store = pg_store
    private, _group = seeded(store)
    other = authorize(store, "qq", OWNER, "private", "10004")
    service = service_for(store)
    page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
    rows = {row["target"]: row for row in page["items"]}

    saved = result(service, "reply_quota.scope_save",
                   {"scope_id": private.id, "account": OWNER, "expected": rows["10002"]["version"],
                    "changes": {"private_daily_replies": 9}})
    assert saved["inherited"] is False and saved["settings"]["private_daily_replies"] == 9
    assert saved["changed"] is True
    assert ReplyPolicies(store).effective(other)["settings"]["private_daily_replies"] == DEFAULTS["private_daily_replies"]

    # A stale page cannot overwrite the newer value.
    assert denied(service, "reply_quota.scope_save",
                  {"scope_id": private.id, "account": OWNER, "expected": rows["10002"]["version"],
                   "changes": {"private_daily_replies": 3}}) == "edit_conflict"
    assert ReplyPolicies(store).effective(private)["settings"]["private_daily_replies"] == 9

    restored = result(service, "reply_quota.scope_reset",
                      {"scope_id": private.id, "account": OWNER, "expected": saved["version"]})
    assert restored["inherited"] is True
    assert restored["settings"]["private_daily_replies"] == DEFAULTS["private_daily_replies"]
    assert restored["changed"] is True
    # Restoring twice does not write another version.
    again = result(service, "reply_quota.scope_reset",
                   {"scope_id": private.id, "account": OWNER, "expected": restored["version"]})
    assert again["changed"] is False and again["version"] == restored["version"]

    defaulted = result(service, "reply_quota.account_save",
                       {"platform": "qq", "account": OWNER, "expected": None,
                        "changes": {"private_daily_replies": 150}})
    assert denied(service, "reply_quota.account_save",
                  {"platform": "qq", "account": OWNER, "expected": None,
                   "changes": {"private_daily_replies": 140}}) == "edit_conflict"
    assert ReplyPolicies(store).get("qq", OWNER)["version"] == defaulted["version"]


def test_saving_the_same_values_pins_an_override_and_inheritance_releases_it(pg_store):
    store = pg_store
    qq_registered(store, private=("10002", "10004"))
    pinned_scope = authorize(store, "qq", OWNER, "private", "10002")
    inheriting = authorize(store, "qq", OWNER, "private", "10004")
    service = service_for(store)

    def rows():
        page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
        return {row["target"]: row for row in page["items"]}

    first = rows()
    assert first["10002"]["inherited"] is True
    assert first["10002"]["settings"] == {"private_daily_replies": DEFAULTS["private_daily_replies"]}
    # Submitting the numbers that are already effective on an inheriting conversation
    # pins them instead of silently doing nothing.
    pinned = result(service, "reply_quota.scope_save",
                    {"scope_id": pinned_scope.id, "account": OWNER, "expected": first["10002"]["version"],
                     "changes": {"private_daily_replies": DEFAULTS["private_daily_replies"]}})
    assert pinned["changed"] is True and pinned["inherited"] is False
    assert pinned["settings"]["private_daily_replies"] == DEFAULTS["private_daily_replies"]

    # A later account default reaches only the conversation that still inherits.
    result(service, "reply_quota.account_save",
           {"platform": "qq", "account": OWNER, "expected": None, "changes": {"private_daily_replies": 77}})
    kept = rows()
    assert kept["10002"]["inherited"] is False
    assert kept["10002"]["settings"]["private_daily_replies"] == DEFAULTS["private_daily_replies"]
    assert kept["10004"]["settings"]["private_daily_replies"] == 77

    # The same submission on an already independent conversation is a no-op.
    again = result(service, "reply_quota.scope_save",
                   {"scope_id": pinned_scope.id, "account": OWNER, "expected": kept["10002"]["version"],
                    "changes": {"private_daily_replies": DEFAULTS["private_daily_replies"]}})
    assert again["changed"] is False and again["version"] == kept["10002"]["version"]

    # Restoring inheritance takes the current account default, not the pinned number.
    restored = result(service, "reply_quota.scope_reset",
                      {"scope_id": pinned_scope.id, "account": OWNER, "expected": kept["10002"]["version"]})
    assert restored["inherited"] is True
    assert restored["settings"]["private_daily_replies"] == 77
    assert ReplyPolicies(store).effective(inheriting)["settings"]["private_daily_replies"] == 77


def test_revoked_registered_conversation_keeps_its_quota_editable(pg_store):
    store = pg_store
    qq_registered(store)
    private = authorize(store, "qq", OWNER, "private", "10002")
    ReplyAuthorizations(store).update(private, enabled=False,
                                      expected_version=ReplyAuthorizations(store).record(private)["version"],
                                      actor="synthetic")
    service = service_for(store)
    page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
    row = page["items"][0]
    assert row["authorized"] is False and row["manual"] is False
    assert "已撤销" in row["status"]
    saved = result(service, "reply_quota.scope_save",
                   {"scope_id": private.id, "account": OWNER, "expected": row["version"],
                    "changes": {"private_daily_replies": 4}})
    assert saved["inherited"] is False and saved["authorized"] is False
    assert ReplyPolicies(store).effective(private)["settings"]["private_daily_replies"] == 4
    # A quota change never grants the conversation again.
    assert ReplyAuthorizations(store).record(private)["enabled"] is False


def test_stale_reset_page_is_refused_even_while_the_scope_already_inherits(pg_store):
    store = pg_store
    private, _group = seeded(store)
    service = service_for(store)
    page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
    row = next(row for row in page["items"] if row["scope_id"] == private.id)
    assert row["inherited"] is True
    # The account default changed after this page was read, so the page is stale.
    ReplyPolicies(store).update("qq", OWNER, {"private_daily_replies": 201}, None, "synthetic")
    reply = call(service, "reply_quota.scope_reset",
                 {"scope_id": private.id, "account": OWNER, "expected": row["version"]})
    assert not reply["ok"] and reply["error"] == "edit_conflict", reply
    # Reading again gives the current version, which is a supported no-op.
    fresh = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
    current = next(row for row in fresh["items"] if row["scope_id"] == private.id)
    again = result(service, "reply_quota.scope_reset",
                   {"scope_id": private.id, "account": OWNER, "expected": current["version"]})
    assert again["changed"] is False and again["inherited"] is True


def test_authorization_states_are_distinguished_from_a_revocation(pg_store):
    from companion_core.reply_audience import Audience, change, read

    store = pg_store
    account_id = wechat_registered(store)
    present = authorize(store, "wechat", account_id, "private", "wxid_friend")
    closed = authorize(store, "wechat", account_id, "private", "wxid_other")
    authorizations = ReplyAuthorizations(store)
    authorizations.update(closed, enabled=False,
                          expected_version=authorizations.record(closed)["version"], actor="synthetic")
    service = service_for(store)
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        authorizations.observe_member(present, False, authorizations.record(present, db=db)["version"], db)

    def rows():
        page = result(service, "reply_quota.scopes",
                      {"platform": "wechat", "account": "wxid_synthetic", "after": ""})
        return {row["scope_id"]: row for row in page["items"]}, page

    listed, _page = rows()
    gone = listed[present.id]
    # The stored tick survives, but the relationship is not verified right now.
    assert gone["manual"] is True and gone["member_present"] is False
    assert gone["authorized"] is False
    assert "已离开或未通过最近核验" in gone["status"]
    revoked = listed[closed.id]
    assert revoked["manual"] is False and revoked["authorized"] is False
    assert "已撤销" in revoked["status"]

    # Turning on allow-all is a policy, not a manual tick and not a revocation.
    category = Audience("wechat", account_id, "private")
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        change(store, category, True, read(store, category, db)["version"], actor="synthetic",
               confirmed=category.describe(), db=db)
    listed, _page = rows()
    allowed = listed[closed.id]
    assert allowed["manual"] is False and allowed["allow_all"] is True
    # Offline an allow-all policy still needs the connector to verify membership.
    assert allowed["authorized"] is False
    assert "允许所有好友已开启" in allowed["status"] and "已撤销" not in allowed["status"]
    assert "已撤销" not in listed[present.id]["status"]

    summary = result(service, "reply_quota.summary", {"after": ""})
    item = summary["accounts"][0]
    # Counts never pass manual ticks off as currently effective grants.
    assert (item["registered"], item["manual"], item["eligible"]) == (2, 1, 0)
    assert item["allow_all"] == {"private": True, "group": False}
    # No permission, list or policy was changed by any of the reads above.
    assert authorizations.record(present)["enabled"] is True
    assert authorizations.record(closed)["enabled"] is False


def test_an_active_stage_that_used_up_its_limits_is_not_current(pg_store):
    store = pg_store
    qq_registered(store, groups=("10003",))
    group = authorize(store, "qq", OWNER, "group", "10003")
    store.set_state("core-run:qq", {"status": "active", "generation": "review-generation"})
    service = service_for(store)
    key = Participation.window_key(group)
    token = ReplyAuthorizations(store).version(group)
    now = service.clock()

    def seed(**values):
        store.set_state(key, {"id": "review-window", "status": "active", "generation": "review-generation",
                              "permission": token, "started": now, "expires": now + 600,
                              "reply_limit": 5, "confirmed": 0, "reserved": 0, "uncertain": 0,
                              "decisions": 0} | values)

    def stage():
        before = store.state(key)
        page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
        row = next(row for row in page["items"] if row["scope_id"] == group.id)
        # Reading the quota page never changes the stored stage.
        assert store.state(key) == before
        return row["window"]

    seed(confirmed=5)
    window = stage()
    assert window["current"] is False and "回复数已用完" in window["note"]
    assert window["used"] == 5 and window["reply_limit"] == DEFAULTS["group_session_replies"]

    seed(confirmed=1, reserved=1, uncertain=1, decisions=20)
    window = stage()
    assert window["current"] is False and "决策次数已用完" in window["note"]
    assert window["used"] == 3 and window["decision_limit"] == DEFAULTS["group_session_decisions"]

    seed(confirmed=1, decisions=1)
    assert stage()["current"] is True

    seed(confirmed=1, expires=now - 1)
    window = stage()
    assert window["current"] is False and "阶段时长" in window["note"]

    seed(confirmed=1)
    store.set_state("core-run:qq", {"status": "active", "generation": "later-generation"})
    window = stage()
    assert window["current"] is False and "运行" in window["note"]


def test_accounts_beyond_the_first_page_stay_readable_and_writable(pg_store):
    store = pg_store
    numbers = ["%d" % (30001 + index) for index in range(51)]
    for uin in numbers:
        qq_registered(store, uin=uin, uid="u_" + uin, private=("40001",), groups=(), selected=False)
        authorize(store, "qq", uin, "private", "40001")
    service = service_for(store)

    def page(after):
        return result(service, "reply_quota.summary", {"after": after})

    first = page("")
    assert first["total"] == 51 and first["offset"] == 0 and len(first["accounts"]) == 20
    second = page(first["next"])
    assert second["offset"] == 20 and len(second["accounts"]) == 20 and second["next"]
    third = page(second["next"])
    assert third["offset"] == 40 and len(third["accounts"]) == 11 and third["next"] is None
    keys = [account["key"] for account in [*first["accounts"], *second["accounts"], *third["accounts"]]]
    assert len(keys) == len(set(keys)) == 51
    assert keys == sorted(keys)
    assert "qq:" + numbers[-1] in keys
    assert denied(service, "reply_quota.summary", {"after": "qq:1"}) == "reply_quota_unknown_account"
    assert denied(service, "reply_quota.summary", {"after": "不 是 游 标"}) == "reply_quota_invalid"

    # The account that the old fixed cap hid is editable once reached by page three.
    last = third["accounts"][-1]
    assert last["account"] == numbers[-1]
    saved = result(service, "reply_quota.account_save",
                   {"platform": "qq", "account": last["account"], "expected": None,
                    "changes": {"private_daily_replies": 8}})
    assert saved["settings"]["private_daily_replies"] == 8


def test_expired_stage_is_closed_first_and_never_reopened_by_a_larger_limit(pg_store):
    store = pg_store
    qq_registered(store, groups=("10003",))
    group = authorize(store, "qq", OWNER, "group", "10003")
    store.set_state(Participation.window_key(group), {
        "id": "stage-expired", "status": "active", "generation": "run-1", "permission": "stale",
        "policy": "old", "started": 1, "expires": 2, "reply_limit": 5,
        "decisions": 1, "confirmed": 1, "reserved": 0, "uncertain": 0})
    service = service_for(store)
    page = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
    row = page["items"][0]
    assert row["window"]["current"] is False and "阶段结束" in row["window"]["note"]
    saved = result(service, "reply_quota.scope_save",
                   {"scope_id": group.id, "account": OWNER, "expected": row["version"],
                    "changes": {"group_session_minutes": 60}})
    assert saved["settings"]["group_session_minutes"] == 60
    window = store.state(Participation.window_key(group))
    assert window["id"] == "stage-expired"
    assert window["status"] == "closed" and window["reason"] == "participation_complete"
    assert window["confirmed"] == 1 and window["reply_limit"] == 5


def test_conversations_are_paged_and_the_cursor_is_validated(pg_store):
    store = pg_store
    targets = [str(200000 + index) for index in range(25)]
    qq_registered(store, private=tuple(targets), groups=())
    for target in targets:
        authorize(store, "qq", OWNER, "private", target)
    service = service_for(store)

    first = result(service, "reply_quota.scopes", {"platform": "qq", "account": OWNER, "after": ""})
    assert first["total"] == 25 and len(first["items"]) == 20 and first["offset"] == 0
    assert first["previous"] is None and first["next"] == first["items"][-1]["scope_id"]
    second = result(service, "reply_quota.scopes",
                    {"platform": "qq", "account": OWNER, "after": first["next"]})
    assert len(second["items"]) == 5 and second["next"] is None and second["offset"] == 20
    assert second["previous"] == ""
    assert denied(service, "reply_quota.scopes",
                  {"platform": "qq", "account": OWNER, "after": secrets.token_hex(16)}) == "reply_quota_unknown_scope"
    assert denied(service, "reply_quota.scopes",
                  {"platform": "qq", "account": OWNER, "after": "x" * 300}) == "reply_quota_invalid"


def test_repeated_request_identifier_does_not_write_twice(pg_store):
    store = pg_store
    seeded(store)
    service = service_for(store)
    request_id = uuid.uuid4().hex
    args = {"platform": "qq", "account": OWNER, "expected": None,
            "changes": {"private_daily_replies": 11}}
    first = result(service, "reply_quota.account_save", args, request_id)
    second = result(service, "reply_quota.account_save", args, request_id)
    assert first == second
    assert len(audit_rows(store, "desktop_reply_quota_account")) == 1
    assert denied(service, "reply_quota.account_save",
                  {**args, "changes": {"private_daily_replies": 12}}, request_id) == "request_id_conflict"
