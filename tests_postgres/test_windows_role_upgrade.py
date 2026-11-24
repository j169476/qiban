"""Upgrade a synthetic v19 data space and replay Windows intake without a second delivery."""

import pytest

from companion_core import privacy
from tests.test_qq_native_normalization import normalize
from tests_postgres.matter_fixture import bound_turn
from tests_postgres.test_qq_module import env as env
from tests_postgres.test_qq_module import handle, incoming
from tests_postgres.test_windows_group_growth import desktop
from tests_postgres.test_windows_role_tools import setup_reminder


@pytest.mark.asyncio
async def test_v19_in_place_upgrade_preserves_existing_user_data_and_no_resend(env):
    from companion_core import reminder_runner, reminders

    saved = env.core.clock()
    alarm = await setup_reminder(env, 1181)
    old = env.model.generate

    async def send(prompt, context, schema, *args, **opts):
        if "clue" in context:
            return bound_turn("合成提醒")
        return await old(prompt, context, schema, *args, **opts)

    from datetime import timedelta

    env.core.clock = lambda: saved + timedelta(minutes=3)
    env.model.generate = send
    await reminder_runner.tick(env.core, env.module)
    assert reminders.listing(env.store, id=alarm["id"])["items"][0]["status"] == "sent"
    docs = {"persona": env.store.read("persona"), "growth": env.store.read("growth")}
    actor = normalize(incoming(1181)).actor
    relationship = dict(
        schema_version=1,
        id=actor.key,
        display_name="合成关系",
        affinity=53,
        interactions=2,
        events=[],
        locked=False,
    )
    existing = env.store.write("relationships", relationship, None, "fixture", actor.key)
    memory = dict(
        schema_version=1,
        id="original",
        kind="experience",
        content="仅本地的合成旧记忆",
        confidence="observed",
        tags=[],
        locked=False,
        source={
            "type": "interaction",
            "reference": normalize(incoming(1181)).id,
            "recorded_at": saved.isoformat(),
        },
    )
    stored = env.store.write("memories", memory, None, "fixture", "original")
    with env.store.db() as db:
        actions = [tuple(r) for r in db.execute("SELECT id,dedupe,status FROM core_actions ORDER BY id")]
        schema = db.execute("SELECT current_schema()").fetchone()[0]
        import re

        assert re.fullmatch("test_[a-f0-9]{32}", schema)
        # Only this disposable fixture schema. Its remaining objects are the
        # actual version-19 tables, including existing events and receipts.
        for table in (
            "role_activity",
            "privacy_corrections",
            "privacy_review_sources",
            "privacy_dependencies",
            "matter_watches",
            "role_matters",
            "matter_contacts",
            "privacy_slices",
            "privacy_jobs",
        ):
            db.execute("DROP TABLE " + table)
        db.execute("DELETE FROM schema_migrations WHERE version>19")
    env.store.migrate()
    env.store.migrate()
    privacy.repair(env.store, env.core.clock().timestamp())
    for k, v in docs.items():
        assert env.store.read(k) == v
    assert (
        env.store.read("relationships", actor.key) == existing
        and env.store.read("memories", "original") == stored
    )
    sent = len(env.provider.sent)
    await handle(env, incoming(1181, text="明天提醒我带报名材料"))
    await desktop(env)._step("qq")
    assert len(env.provider.sent) == sent
    with env.store.db() as db:
        assert [
            tuple(r) for r in db.execute("SELECT id,dedupe,status FROM core_actions ORDER BY id")
        ] == actions
        assert (
            db.execute("SELECT complete FROM privacy_jobs WHERE resource='memories/original'").fetchone()[0]
            == 0
        )
