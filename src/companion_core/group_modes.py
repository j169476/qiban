"""Per-group observation and voluntary participation. Neither grants reply permission."""
from datetime import datetime, time, timedelta
from math import ceil
from uuid import uuid4
from zoneinfo import ZoneInfo

from .errors import AssistantError
from .reply_authorization import ReplyAuthorizations
from .reply_exclusion import ReplyExclusions, accepts

DAILY_DECISIONS = 4
COOLDOWN = 15 * 60


def settings(store, scope, db=None):
    return store.state("group-mode:" + scope.id, {
        "listen": False, "proactive": False, "since": None, "version": None}, db)


def save(store, scope, expected, listen, proactive, now, db):
    if scope.kind != "group" or type(listen) is not bool or type(proactive) is not bool:
        raise AssistantError("management_request", "群设置无效")
    previous = settings(store, scope, db)
    if previous["version"] != expected:
        raise AssistantError("edit_conflict", "该群设置已经变化，请重新读取")
    proactive = listen and proactive
    if (listen, proactive) == (previous["listen"], previous["proactive"]):
        return previous
    value = {"listen": listen, "proactive": proactive, "version": uuid4().hex,
             "since": previous["since"] if previous["listen"] and listen else ceil(now)}
    store.set_state("group-mode:" + scope.id, value, db)
    db.execute("DELETE FROM state WHERE key=?", ("group-samples:" + scope.id,))
    return value


def observing(store, scope, stamp=None, db=None):
    if scope.kind != "group":
        return False
    mode = settings(store, scope, db)
    return (mode["listen"] and (stamp is None or stamp >= mode["since"])
            and (accepts(store, scope, stamp, db=db) if stamp is not None else
                 not ReplyExclusions(store).record(scope, db=db)["excluded"])
            and ReplyAuthorizations(store).record(scope, db=db).get("member_present", True))


def observe_event(store, scope, event, db):
    if scope.kind != "group" or event.visibility != "group":
        return
    stamp = datetime.fromisoformat(event.occurred_at).timestamp()
    if (event.metadata.get(event.platform, {}).get("identity_quality") == "verified"
            and observing(store, scope, stamp, db)):
        from .expression_learning import observe
        from .group_learning import sample
        observe(store, event.platform, event.thread, event.id, event.text, db)
        sample(store, scope, event, stamp, db)


def voluntary(participation, scope, event_id, message_time, db):
    from . import events
    from .contracts import Event
    from .role_affinity import close_contact

    store, now = participation.store, participation.now()
    mode = settings(store, scope, db)
    if (not mode["proactive"] or message_time is None or not 0 <= now - message_time <= 300
            or not observing(store, scope, message_time, db)):
        return None
    if initiative_slot(store, scope, participation.clock(), db, reserve=False) > now:
        return None
    event = Event.from_dict(events.get(store, event_id)["event"])
    if (event.platform != "qq" or scope.platform != "qq" or event.account != scope.account_identity
            or event.visibility != "group" or event.metadata.get("qq", {}).get("target_id") != scope.target_id
            or not close_contact(store, event)):
        return None
    initiative_slot(store, scope, participation.clock(), db, reserve=True)
    return mode["version"]


def initiative_slot(store, scope, instant, db, *, reserve):
    local = instant.astimezone(ZoneInfo(store.read("policy")["data"]["timezone"]))
    day, now = local.date().isoformat(), instant.timestamp()
    key = "group-voluntary:" + scope.id
    budget = store.state(key, {}, db)
    used = budget.get("used", 0) if budget.get("day") == day else 0
    earliest = budget.get("next", 0)
    if used >= DAILY_DECISIONS:
        tomorrow = datetime.combine(local.date() + timedelta(days=1), time(), local.tzinfo)
        earliest = max(earliest, tomorrow.timestamp())
    if reserve and earliest <= now:
        store.set_state(key, {"day": day, "used": used + 1, "next": now + COOLDOWN}, db)
    return earliest
