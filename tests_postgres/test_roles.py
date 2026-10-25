import secrets

import psycopg
import pytest
from psycopg import sql

from companion_core.errors import AssistantError
from companion_core.pg_roles import provision
from companion_core.postgres import PostgresStore


def test_application_cannot_change_schema_or_read_other_schemas(pg_store, tmp_path):
    options = pg_store.database._options
    schema = options["options"].split("search_path=", 1)[1]
    role, password = "app_test_" + secrets.token_hex(8), secrets.token_urlsafe(36)
    app = None
    with psycopg.connect(**options) as owner:
        provision(owner, schema=schema, role=role, password=password)
    try:
        app = PostgresStore(tmp_path / "app", {**options, "user": role, "password": password}, secrets.token_bytes(32))
        app.set_state("app-can-write", {"receipt": True})
        assert app.state("app-can-write") == {"receipt": True}
        with pytest.raises(AssistantError):
            with app.db() as db:
                db.execute("CREATE TABLE unauthorized(id INTEGER)")
        with pytest.raises(AssistantError):
            with app.db() as db:
                db.execute("UPDATE schema_migrations SET checksum='changed'")
        with pytest.raises(AssistantError):
            with app.db() as db:
                db.execute("SELECT * FROM pg_authid")
        with app.db() as db:
            row = db.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
            assert not any(row)
    finally:
        if app:
            app.database.close()
        with psycopg.connect(**options, autocommit=True) as owner:
            owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
