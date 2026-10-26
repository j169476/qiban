"""A generation-bound, one-attempt Bilibili private-reply test scope."""
import re
from datetime import datetime

from .errors import AssistantError


def validate(selection, accounts, *, read_only):
    if selection is None:
        return None
    if (not isinstance(selection, dict) or set(selection) != {"account_uid", "target_uid"}
            or any(not isinstance(v, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", v)
                   for v in selection.values())
            or accounts != [["bilibili", selection.get("account_uid")]]
            or selection["account_uid"] == selection["target_uid"] or read_only):
        raise AssistantError("reply_test_invalid", "限定私信验收需要匹配的B站机器人UID、不同的对象UID及非只读运行")
    return dict(selection)


def allows(run, event):
    scope = run.get("reply_test")
    if scope is None:
        return True
    try:
        fresh = datetime.fromisoformat(event.occurred_at) >= datetime.fromisoformat(run["started_at"])
    except (ValueError, TypeError):
        return False
    return (event.platform == "bilibili" and event.account == scope["account_uid"]
            and event.actor.platform == "bilibili" and event.actor.user_id == scope["target_uid"]
            and event.visibility == "private" and event.metadata.get("kind") == "dm_reply" and fresh)


def require_event(run, event):
    if not allows(run, event):
        raise AssistantError("reply_test_scope", "本次验收只处理指定对象在启动后发送的真实私信")


def attempt_key(run):
    return "reply-test-attempt:" + run["generation"]


def require_action(store, run, action, payload, db, *, reserve=False):
    """Re-read the native event; reserve once in the same dispatch transaction."""
    scope = run.get("reply_test")
    if scope is None:
        return
    current = store.state("core-run:bilibili", {}, db)
    if (current.get("generation") != run["generation"] or current.get("status") != "active"
            or current.get("reply_test") != scope or action["kind"] != "dm_reply"
            or action["account"] != scope["account_uid"]):
        raise AssistantError("reply_test_scope", "本次验收禁止其他账号、对象或互动动作")
    event = db.execute("SELECT uid,kind,received,body FROM events WHERE id=? AND account=?",
                       (payload.get("event_id"), scope["account_uid"])).fetchone()
    native = store.ledger_decode("events.body", event["body"]) if event else {}
    if (not event or event["kind"] != "dm_reply" or event["uid"] != scope["target_uid"]
            or datetime.fromisoformat(event["received"]) < datetime.fromisoformat(run["started_at"])
            or native.get("uid") != scope["target_uid"] or native.get("kind") != "dm_reply"
            or native.get("id") != payload.get("event_id") or native.get("received") != event["received"]
            or payload.get("_event") != native):
        raise AssistantError("reply_test_scope", "发送对象必须对应指定UID启动后的真实入站私信")
    claimed = store.state(attempt_key(run), db=db)
    if reserve:
        if claimed is not None:
            raise AssistantError("reply_test_exhausted", "本次验收的一次外发尝试已经使用，不自动重试")
        store.set_state(attempt_key(run), {"action_id": action["id"]}, db)
    elif claimed != {"action_id": action["id"]}:
        raise AssistantError("reply_test_exhausted", "该动作没有本次验收唯一的派发预留")
