import pytest

from bili_assistant.conversations import record_action
from companion_core import conversations
from companion_core.governance import GovernanceRepository
from companion_core.public_workspace import PublicWorkspace


def test_archive_scope_and_receipt_rollback_together(pg_store):
    def reject(_):
        raise RuntimeError("synthetic scope write failure")

    args = (pg_store, "123", "local", "owner", "inbound-1", "private", "user", "owner",
            "received", "2026-10-23T00:00:00+00:00")
    with pytest.raises(RuntimeError, match="scope write failure"):
        conversations.append(*args, on_write=reject)
    assert not pg_store.records("conversation_turns")[0]
    assert len(conversations.append(*args)) == 1
    assert conversations.append(*args) == []


def test_public_recovery_does_not_promote_unproven_private_reply(pg_store):
    event = {"id": "dm-11", "uid": "11", "kind": "dm_reply", "received": "2026-10-23T00:00:00+00:00",
             "text": "private input"}
    with pg_store.db() as db:
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (event["id"], "123", "11", "dm_reply",
                   event["received"], pg_store.ledger_encode("events.body", event)))
    action = {"id": "old-private", "kind": "dm_reply", "account": "123", "status": "succeeded",
              "created": event["received"], "dispatched": event["received"],
              "payload": pg_store.ledger_encode("actions.payload", {"event_id": event["id"], "text": "private reply"})}
    workspace = PublicWorkspace(pg_store)
    ids = record_action(workspace, action)
    assert len(ids) == 1
    resource = "conversation_turns/" + ids[0]
    assert GovernanceRepository(pg_store).policy(resource).classification == "unclassified"
    assert not workspace.access.can_read(workspace.request, resource)
    assert record_action(workspace, action) == []
