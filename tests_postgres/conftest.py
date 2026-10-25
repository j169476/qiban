import json
import os
import re
import secrets
import uuid
from pathlib import Path

import psycopg
import pytest

from companion_core.postgres import PostgresStore


@pytest.fixture
def pg_profile():
    return None


@pytest.fixture
def pg_store(tmp_path, pg_profile):
    config = Path(os.environ.get("COMPANION_TEST_DATABASE", "")) if os.environ.get("COMPANION_TEST_DATABASE") else (
        Path(__file__).resolve().parents[1] / ".dev/postgres-test-v2/connection.json")
    if not config.exists():
        if os.environ.get("CI") or os.environ.get("COMPANION_REQUIRE_POSTGRES") == "1":
            pytest.fail("Required isolated PostgreSQL configuration is missing", pytrace=False)
        pytest.skip("Explicit local PostgreSQL test instance is not provisioned")
    options = json.loads(config.read_text(encoding="utf-8"))
    assert options.get("dbname") == "companion_test" and options.get("host") == "127.0.0.1"
    schema = "test_" + uuid.uuid4().hex
    assert re.fullmatch(r"test_[a-f0-9]{32}", schema)
    try:
        with psycopg.connect(**options, autocommit=True) as db:
            db.execute('CREATE SCHEMA "' + schema + '"')
    except psycopg.Error:
        pytest.fail("Dedicated synthetic PostgreSQL instance is unavailable", pytrace=False)
    try:
        scoped = {**options, "options": "-c search_path=" + schema}
        store = PostgresStore(tmp_path / "data", scoped, secrets.token_bytes(32), profile=pg_profile)
        store.migrate()
        assert store.initialize()["ok"]
        yield store
    finally:
        if "store" in locals():
            store.database.close()
        # Only the fresh synthetic schema above; never an externally supplied name.
        with psycopg.connect(**options, autocommit=True) as db:
            db.execute('DROP SCHEMA "' + schema + '" CASCADE')
