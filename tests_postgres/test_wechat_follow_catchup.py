"""Real source cursor + encrypted inbox and role, with no external model or send."""

import sqlite3
from contextlib import closing, nullcontext
from datetime import datetime, timezone

import pytest

from companion_core.errors import AssistantError
from companion_core.reply_audience import Audience
from companion_wechat.db_probe import message_table
from tests_postgres.test_reply_audiences import set_mode
from tests_postgres.test_wechat_module import compose
from tests_wechat.test_db_source import GROUP, OWNER, SELF, SINCE, Harness


@pytest.mark.parametrize("group", [False, True], ids=["friend", "group"])
async def test_new_and_returning_member_catches_up_from_original_grant(pg_store, tmp_path, group):
    lab = Harness(tmp_path)
    target, kind = (GROUP, "group") if group else (OWNER, "private")
    lab.targets = [t for t in lab.targets if t.username != target]
    conn = lab.shards["message_0.db"]
    table = message_table(target)
    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN source')

    def insert(number, created, text):
        conn.execute(
            f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?,?)',
            (
                number,
                1000 + number,
                created,
                1,
                3 if group else 1,
                text,
                9,
                f"<msgsource><atuserlist>{SELF}</atuserlist></msgsource>",
            ),
        )

    with closing(sqlite3.connect(":memory:", isolation_level=None)) as contacts, lab.session() as parent:
        contacts.execute(
            "CREATE TABLE contact(username,local_type,flag,delete_flag,verify_flag,is_in_chat_room)"
        )
        contacts.execute("INSERT INTO contact VALUES (?,?,1,0,0,?)", (target, 2 if group else 1, int(group)))
        parent._prepared.open_contacts = lambda: nullcontext(contacts)
        parent._prepared.shards = {lab.account / "db_storage/message/message_0.db": conn}
        env = await compose(
            pg_store,
            previous=parent.snapshot(),
            authorized=False,
            family=True,
            clock=lambda: datetime.fromtimestamp(SINCE + 100, timezone.utc),
        )
        receiver = env.receiver
        receiver.fixed_windows = receiver.monitor_members = True
        category = Audience("wechat", receiver.bindings.identity, kind)
        policy = set_mode(pg_store, category, True)
        pg_store.set_state(category.key, {**policy, "since": SINCE})
        env.driver.members.add(target)

        async def request(command, **args):
            if command == "chat_member_page":
                return parent.member_page(**args, find_chats=True)
            if command == "member_window":
                args["limit"] = 1
            return getattr(parent, command)(**args)

        env.worker.request = request
        clock = [0.0]
        receiver.clock = lambda: clock[0]

        async def poll():
            clock[0] += 6
            return await env.adapter.collect_once()

        async def pending():
            for _ in range(6):
                events = await poll()
                if events:
                    assert len(events) == 1
                    return events[0]
            pytest.fail("window never became eligible")

        try:
            env.core.start()
            insert(1, SINCE - 1, "excluded")
            insert(2, SINCE, "already delivered")
            first = await pending()
            assert (await env.core.handle(first))["action"]["status"] == "succeeded"
            insert(3, SINCE + 1, "pending before removal")
            parked = await pending()
            contacts.execute("UPDATE contact SET delete_flag=1")
            assert await poll() == []
            with pytest.raises(AssistantError):
                env.session.check_event(parked)
            insert(4, SINCE + 2, "visible backlog")
            insert(5, SINCE + 3, "another page")
            assert await poll() == []
            contacts.execute("UPDATE contact SET delete_flag=0")
            returned = await pending()
            batch = env.session.merged.for_event(returned)
            assert batch["catchup"] and len(batch["members"]) == 3
            assert (await env.core.handle(returned))["action"]["status"] == "succeeded"
            assert len(env.driver.sent) == 2
            for _ in range(3):
                assert await poll() == []
            position = receiver.follow.position(target)
            assert position["checkpoint"]["sources"] and position["window"] is None
            # A worker restart loses the live membership token but not the position
            # or send receipts; the next pass must not replay the completed range.
            receiver.follow_directory.disconnect()
            assert await poll() == []
            assert receiver.follow.position(target)["checkpoint"] == position["checkpoint"]
            assert len(env.driver.sent) == 2
            set_mode(pg_store, category, False)
            assert await poll() == []
            renewed = set_mode(pg_store, category, True)
            pg_store.set_state(category.key, {**renewed, "since": SINCE + 5})
            insert(6, SINCE + 4, "before second grant")
            insert(7, SINCE + 5, "after second grant")
            final = await pending()
            assert len(env.session.merged.for_event(final)["members"]) == 1
            assert (await env.core.handle(final))["action"]["status"] == "succeeded"
            assert len(env.driver.sent) == 3
            insert(8, SINCE + 6, "membership changes after generation")
            late = await pending()

            async def leave_and_return(_request):
                live = receiver.follow_directory.live[category]
                live.remove(target)
                live.replace(renewed["version"], {target: target})

            env.driver.verify_membership = leave_and_return
            outcome = await env.core.handle(late)
            assert outcome["action"]["status"] != "succeeded" and len(env.driver.sent) == 3
            assert await poll() == []
        finally:
            env.core.stop()
            await env.adapter.close()
    lab.close()
