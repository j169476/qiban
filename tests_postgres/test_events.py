from dataclasses import replace

import pytest

from companion_core import events
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError


def test_private_ledger_encryption_routing_and_changed_version(pg_store):
    store = pg_store
    first = Event("alpha", "bot", "first", ActorRef("alpha", "user"), "room", "2026-10-23T04:00:00Z", "private-canary")
    second = replace(first, native_id="second", conversation="other")
    for event in (first, second):
        events.ingest(store, event)
    source = events.get(store, first.id)
    assert source["event"]["text"] == "private-canary"
    store.set_state("core-delivery:" + first.id, {"version": source["version"], "owner": "control", "status": "deferred",
                    "due_at": "2026-10-24T00:00:00Z", "config_token": "config"})
    kwargs = {"owner": "control", "now": "2026-10-23T12:00:00+08:00", "config_token": "config", "limit": 1}
    assert events.pending(store, [["alpha", "bot"]], "2026-10-23T03:00:00Z", **kwargs) == [second]
    assert events.recent(store, conversation=first.thread)[0] == source
    store.set_state("core-processed:" + second.id, events.get(store, second.id)["version"])
    assert not events.pending(store, [["alpha", "bot"]], "2026-10-23T03:00:00Z", **kwargs)
    changed = replace(first, text="corrected-canary")
    events.ingest(store, changed)
    assert events.pending(store, [["alpha", "bot"]], "2026-10-23T03:00:00Z", **kwargs) == [changed]
    store.set_state("private-state", {"message": "private-canary"})
    with store.db() as db:
        assert all("canary" not in row[0] for row in db.execute("SELECT body FROM core_events"))
        raw = db.execute("SELECT value FROM state WHERE key='private-state'").fetchone()[0]
        assert "canary" not in raw
        with pytest.raises(AssistantError):
            store.ledger_decode("state:wrong-key", raw)
    with pytest.raises(AssistantError):
        with store.db() as db:
            db.execute("UPDATE state SET value=? WHERE key='private-state'", ('{"message":"plaintext"}',))
