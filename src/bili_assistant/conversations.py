"""Editable turn archives. Raw conversations are scoped, never a global memory catalog."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from companion_core.conversations import append, context, identifier, records, thread_id

from .errors import AssistantError


def event_thread(event):
    if event["kind"] == "dm_reply":
        return "dm", event["uid"]
    # A missing root must not collapse unrelated comments into a video-wide thread.
    root = event.get("root") or event.get("parent") or event["id"]
    resource = event.get("resource_type", 1)
    prefix = "" if resource == 1 else f"{resource}:"
    return "comment", f"{prefix}{event.get('oid') or event.get('bvid', '')}:{root}"




def record_event(store, account, event):
    from companion_core.governance import GovernedContext, enabled
    from companion_core.public_workspace import PublicWorkspace

    # Ingress is classified from its verified transport, never from a public
    # exploration workspace that happens to be running when the inbox arrives.
    actual = store.store if isinstance(store, PublicWorkspace) else store
    classify = None
    if enabled(actual):
        from .connector import normalize

        context = GovernedContext(actual, normalize(account, event))
        context.classify_event()

        def classify(item_id):
            context.classify_written("conversation_turns", item_id, sources=["event/" + context.event.id])

    channel, key = event_thread(event)
    serial = re.search(r"([0-9]+)$", event["id"])
    result = append(
        actual, account, channel, key, event["id"], event.get("text", ""), "user", event["uid"],
        "received", event["received"], event["received"], str(event.get("parent") or ""),
        sequence=int(serial[1]) if serial else 0,
        attachments=event.get("attachments", []), on_write=classify,
    )
    store.set_state(f"conversation-event-archived:{account}:{event['id']}", True)
    return result


def record_action(store, action):
    """Only reply/comment attempts belong to conversation history; drafts do not."""
    from companion_core.governance import record_provenance
    from companion_core.public_workspace import PublicWorkspace

    # Recovery may be requested during public exploration. A missing historical
    # proof must remain quarantined, never inherit that workspace's visibility.
    store = store.store if isinstance(store, PublicWorkspace) else store
    if action["kind"] not in ("reply", "dm_reply", "comment"):
        return []
    if action["status"] not in ("succeeded", "failed", "uncertain", "cancelled"):
        return []
    payload = store.ledger_decode("actions.payload", action["payload"])
    reply_to = payload.get("event_id", "")
    with store.db() as db:
        event = db.execute("SELECT body FROM events WHERE account=? AND id=?",
                           (action["account"], reply_to)).fetchone() if reply_to else None
    if reply_to and not event:
        return []
    if event:
        channel, key = event_thread(store.ledger_decode("events.body", event["body"]))
    else:
        result = (store.ledger_decode("actions.result", action["result"]) if action.get("result") else {})
        # Use the actual root rpid. If unknown, do not join a guessed comment thread.
        rpid = result.get("rpid") or (result.get("data") or {}).get("rpid")
        if not rpid:
            return []
        from bilibili_api.utils.aid_bvid_transformer import bvid2aid

        channel, key = "comment", f"{bvid2aid(payload['bvid'])}:{rpid}"
    proof = store.state("action-provenance:" + action["id"])
    result = append(
        store, action["account"], channel, key, "action:" + action["id"], payload["text"],
        "assistant", action["account"],
        "sent" if action["status"] == "succeeded" else action["status"],
        action["created"], action.get("dispatched"), reply_to, sequence=1,
        on_write=(lambda item_id: record_provenance(store, "conversation_turns", item_id, proof)) if proof else None,
    )
    store.set_state(f"conversation-action-archived:{action['id']}", True)
    return result


def recover_platform_turns(engine):
    """Reconcile only missing archive writes after a crash; never dispatch anything."""
    account = engine.store.read("manifest")["data"]["account_uid"]
    if not account:
        return
    with engine.store.db() as db:
        events = db.execute("""SELECT body FROM events WHERE account=? AND NOT EXISTS (
            SELECT 1 FROM state WHERE key='conversation-event-archived:' || events.account || ':' || events.id)
            ORDER BY received""", (account,)).fetchall()
        actions = db.execute(
            """SELECT * FROM actions WHERE account=? AND kind IN ('reply','dm_reply','comment')
               AND status IN ('succeeded','failed','uncertain','cancelled') AND NOT EXISTS (
               SELECT 1 FROM state WHERE key='conversation-action-archived:' || actions.id) ORDER BY created""",
            (account,),
        ).fetchall()
    for row in events:
        record_event(engine.store, account, engine.store.ledger_decode("events.body", row["body"]))
    for row in actions:
        record_action(engine.store, dict(row))






def context_for_events(store, account, events, *, queries=()):
    ids = list(dict.fromkeys(thread_id(account, *event_thread(e)) for e in events))
    remaining = store.read("companion")["data"].get("conversation_context_chars", 12000)
    result = []
    for index, tid in enumerate(ids):
        query = " ".join(event.get("text", "") for event in events if thread_id(account, *event_thread(event)) == tid)
        entry = context(store, account, tid, remaining // (len(ids) - index), query=query, queries=queries)
        remaining -= entry["context_chars"]
        result.append(entry)
    return result


def sync_codex(engine):
    """Import only the bound role chat through the public, paginated host API."""
    engine.mode("role")
    account = engine.store.read("manifest")["data"]["account_uid"]
    if not account:
        raise AssistantError("account_not_bound", "先绑定账号，再同步角色对话")
    key = engine.identity()
    marker = f"conversation-sync:{account}:{key}"
    progress = engine.store.state(marker, {"cursor": None, "sequence": 0})
    imported = 0
    complete = False
    for _ in range(3):
        page = engine.host.conversation_page(progress["cursor"])
        sequence = progress["sequence"]
        unfinished = False
        for turn in page["data"]:
            unfinished = turn["status"] not in ("completed", "failed", "interrupted")
            if turn.get("itemsView", "full") != "full":
                raise AssistantError("host_history_incomplete", "宿主只返回对话摘要，未冒充原文保存")
            for index, item in enumerate(turn["items"]):
                speaker, content = None, ""
                if item["type"] == "userMessage":
                    speaker = "user"
                    content = "\n".join(p["text"] for p in item["content"] if p["type"] == "text")
                elif item["type"] == "agentMessage" and not unfinished and (
                    item.get("phase") in ("final_answer", "partial_answer")
                    or item.get("phase") is None and turn["status"] == "completed"
                ):
                    speaker, content = "assistant", item["text"]
                if not speaker or not content:
                    continue
                timestamp = turn.get("startedAt" if speaker == "user" else "completedAt")
                when = datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else None
                imported += len(append(
                    engine.store, account, "codex", key, item["id"], content,
                    speaker, account if speaker == "assistant" else "",
                    "received" if speaker == "user" else "sent", engine.clock().isoformat(), when,
                    turn["id"], sequence + index,
                ))
            sequence += len(turn["items"])
            if unfinished:
                break  # Users' current input is real; unfinished assistant drafts are not.
        if unfinished or not page.get("nextCursor"):
            complete = not unfinished
            break  # Keep the start of the last page to catch newly completed turns next time.
        progress = {"cursor": page["nextCursor"], "sequence": sequence}
        engine.store.set_state(marker, progress)
    return {"imported": imported, "caught_up": complete, "awaiting_completion": unfinished,
            "more_history": bool(page.get("nextCursor")) and not unfinished,
            "thread_id": thread_id(account, "codex", key)}

__all__ = ['identifier', 'records']
