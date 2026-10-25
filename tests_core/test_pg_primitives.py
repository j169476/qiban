import secrets
from types import SimpleNamespace

import pytest

from companion_core.cipher import ContentCipher
from companion_core.errors import AssistantError
from companion_core.pg_connection import Row, parameters, row_factory


def test_cipher_binds_content_to_kind_id_and_revision():
    cipher = ContentCipher(secrets.token_bytes(32))
    encrypted = cipher.encrypt(b"PRIVATE", "memory", "a", "version")
    assert cipher.decrypt(encrypted, "memory", "a", "version") == b"PRIVATE"
    assert b"PRIVATE" not in encrypted
    for key in (("memory", "b", "version"), ("memory", "a", "old"), ("other", "a", "version")):
        with pytest.raises(AssistantError):
            cipher.decrypt(encrypted, *key)
    with pytest.raises(AssistantError):
        cipher.decrypt(encrypted[:-1] + bytes((encrypted[-1] ^ 1,)), "memory", "a", "version")


def test_sql_parameters_preserve_literals():
    assert parameters("SELECT '?' AS x,? AS y WHERE text LIKE 'a%' AND x='it''s?' ") == (
        "SELECT '?' AS x,%s AS y WHERE text LIKE 'a%%' AND x='it''s?' ")
    row = Row(["id", "value"], ["a", "b"])
    assert row[0] == row["id"] == "a" and dict(row) == {"id": "a", "value": "b"}
    assert dict([row]) == {"a": "b"}


def test_rows_share_an_immutable_column_map_without_sharing_values():
    factory = row_factory(SimpleNamespace(description=[SimpleNamespace(name="id"), SimpleNamespace(name="value")]))
    first, second = factory(["a", 1]), factory(["b", 2])
    assert first.index is second.index
    assert dict(first) == {"id": "a", "value": 1} and dict(second) == {"id": "b", "value": 2}
    with pytest.raises(TypeError):
        first.index["id"] = 1
    assert second["id"] == "b"


def test_reused_statement_templates_keep_literal_and_parameter_semantics():
    cases = [("SELECT ?, '?'", "SELECT %s, '?'"), ('SELECT "?", ?', 'SELECT "?", %s'),
             ("SELECT 'it''s?', ?", "SELECT 'it''s?', %s"), ("SELECT '50%', ?", "SELECT '50%%', %s")]
    for _ in range(3):
        for query, expected in cases:
            assert parameters(query) == expected
    for _ in range(2):
        with pytest.raises(AssistantError):
            parameters("SELECT 'unfinished")
