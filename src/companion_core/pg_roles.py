"""Provision an application role through an explicitly privileged connection."""
import re

from .errors import AssistantError


def provision(connection, *, schema, role, password):
    from psycopg import sql

    if (not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", role)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema)
            or not isinstance(password, str) or len(password) < 32):
        raise AssistantError("database_role", "数据库应用角色参数无效")
    found = connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
    if found:
        raise AssistantError("database_role", "应用角色已经存在，不能覆盖其口令或权限")
    role_id, schema_id = sql.Identifier(role), sql.Identifier(schema)
    connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS")
                       .format(role_id, sql.Literal(password)))
    connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_id, role_id))
    connection.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(schema_id, role_id))
    connection.execute(sql.SQL("GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA {} TO {}").format(schema_id, role_id))
    # Checked migrations/contract schemas are read-only to normal application code.
    for table in ("schema_migrations", "record_kinds"):
        connection.execute(sql.SQL("REVOKE INSERT,UPDATE,DELETE ON {}.{} FROM {}")
                           .format(schema_id, sql.Identifier(table), role_id))
    return {"role": role, "schema": schema, "ddl_allowed": False, "bypass_rls": False}
