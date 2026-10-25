import json

import psycopg
import pytest
from psycopg import sql

from companion_core.errors import AssistantError
from tests_postgres.test_provenance import OTHER, OWNER, RESOURCE, aggregate, request


@pytest.mark.parametrize("column,value", [
    ("classification", "secret"), ("owners", json.dumps([OTHER.key])), ("valid", 0), ("expires_at", 1),
    ("sources", '["event/unseen"]'), ("source_versions", '[["event/unseen","v1"]]'),
])
def test_warm_parse_cache_observes_external_changes_even_without_version_bump(pg_store, column, value):
    admin, _, _, snapshot = aggregate(pg_store, source=True)
    lease = admin.access.lease(request(admin), [snapshot])
    admin.access.revalidate(lease)
    old = admin.repository.policy("event/source")
    # A separately committed connection models another process or damaged import.
    with psycopg.connect(**pg_store.database._options) as db:
        db.execute(sql.SQL("UPDATE governance_policies SET {}=%s WHERE resource=%s").format(sql.Identifier(column)),
                   (value, "event/source"))
    assert admin.repository.policy("event/source").version == old.version
    with pytest.raises(AssistantError):
        admin.access.revalidate(lease)
    assert not admin.access.can_read(request(admin), snapshot)


def test_warm_parse_cache_does_not_keep_revoked_grants_or_virtual_registrations(pg_store):
    admin, _, _, snapshot = aggregate(pg_store)
    grant = admin.authorize(request(admin, administrator=True), grantee=OTHER.key, owner=OWNER.key, resources=[RESOURCE])
    foreign = admin.access.lease(request(admin, OTHER), [snapshot])
    admin.access.revalidate(foreign)
    admin.revoke(request(admin, administrator=True), grant["id"])
    with pytest.raises(AssistantError):
        admin.access.revalidate(foreign)
    owner = admin.access.lease(request(admin), [snapshot])
    with pg_store.db() as db:
        db.execute("DELETE FROM governance_provenance WHERE resource=?", (snapshot,))
    with pytest.raises(AssistantError):
        admin.access.revalidate(owner)


def test_rolled_back_policy_read_cannot_poison_later_fresh_reads(pg_store):
    admin, _, _, snapshot = aggregate(pg_store, source=True)
    lease = admin.access.lease(request(admin), [snapshot])
    with pytest.raises(RuntimeError), pg_store.db() as db:
        db.execute("UPDATE governance_policies SET owners=? WHERE resource=?", (json.dumps([OTHER.key]), "event/source"))
        assert admin.repository.policy("event/source").owners == {OTHER.key}
        raise RuntimeError("rollback synthetic mutation")
    assert admin.repository.policy("event/source").owners == {OWNER.key}
    admin.access.revalidate(lease)


def test_expiry_is_checked_again_when_parsed_fields_are_unchanged(pg_store):
    admin, _, _, snapshot = aggregate(pg_store, source=True)
    clock = [admin.authority.clock()]
    admin.authority.clock = lambda: clock[0]
    with pg_store.db() as db:
        db.execute("UPDATE governance_policies SET expires_at=? WHERE resource=?", (clock[0] + 30, "event/source"))
    lease = admin.access.lease(request(admin), [snapshot])
    admin.access.revalidate(lease)
    clock[0] += 31
    with pytest.raises(AssistantError):
        admin.access.revalidate(lease)


def test_checkout_still_resets_both_timeouts_and_local_deadlines(pg_store):
    with pg_store.db() as db:
        db.execute("SET statement_timeout='25s'")
        db.execute("SET lock_timeout='20s'")
    with pg_store.db() as db:
        assert db.execute("SHOW statement_timeout").fetchone()[0] == "15s"
        assert db.execute("SHOW lock_timeout").fetchone()[0] == "5s"
        db.execute("SELECT set_config('statement_timeout','1000',true)")
    with pg_store.db() as db:
        assert db.execute("SHOW statement_timeout").fetchone()[0] == "15s"


def test_checkout_keeps_transaction_isolation_selectable_before_first_query(pg_store):
    with pg_store.db() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        assert db.execute("SHOW transaction_isolation").fetchone()[0] == "repeatable read"
        assert db.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        assert db.execute("SHOW statement_timeout").fetchone()[0] == "15s"
        assert db.execute("SHOW lock_timeout").fetchone()[0] == "5s"


def test_checkout_initialization_failure_clears_thread_connection(pg_store, monkeypatch):
    original = psycopg.Connection.execute
    failed = False

    def fail_once(connection, query, *args, **kwargs):
        nonlocal failed
        if not failed and isinstance(query, str) and query.startswith("SET statement_timeout='15s';"):
            failed = True
            raise psycopg.OperationalError("synthetic initialization failure")
        return original(connection, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", fail_once)
    with pytest.raises(AssistantError, match="数据库操作未完成"), pg_store.db():
        pass
    assert pg_store.database._local.connection is None
    with pg_store.db() as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1
