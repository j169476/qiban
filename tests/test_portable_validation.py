"""Isolated coverage of the portable snapshot validator.

Every case builds a synthetic ``postgres_transfer.unpack``-shaped structure with a random
32-byte data key. No database, network, credential or existing file is touched.
"""
import base64
import copy
import hashlib
import json
import re
import secrets
from pathlib import Path

import pytest
import yaml

from companion_core import legacy_import, postgres_transfer
from companion_core.cipher import ContentCipher
from companion_core.errors import AssistantError
from companion_core.portable_validation import (
    CIPHER_PAYLOADS,
    LEDGER_PAYLOADS,
    LEGACY_LEDGER_PAYLOADS,
    validate_snapshot,
)

KEY = secrets.token_bytes(32)
WRONG_KEY = secrets.token_bytes(32)
TABLES = postgres_transfer.TABLES
# The migrations installed beside the module that owns TABLES, never a stale copy.
SQL = Path(postgres_transfer.__file__).resolve().parent / "sql"
REGISTERED = sorted(field.path for field in (*CIPHER_PAYLOADS, *LEDGER_PAYLOADS))
NEEDED = {}
for _field in (*CIPHER_PAYLOADS, *LEDGER_PAYLOADS, *LEGACY_LEDGER_PAYLOADS):
    NEEDED.setdefault(_field.table, set()).update(_field.needs)


class Profile:
    """Minimal data profile: the current schema requires one string field per kind."""

    def __init__(self, extra_tables=()):
        self.extra_tables = set(extra_tables)
        self.schemas = {"memories": ("content",), "manifest": ("project_id",)}
        self.validated = []

    def validate(self, kind, data):
        fields = self.schemas.get(kind)
        if fields is None:
            raise AssistantError("schema_unknown", kind)
        if not isinstance(data, dict) or any(not isinstance(data.get(name), str) for name in fields):
            raise AssistantError("invalid_data", "record does not satisfy the current schema")
        self.validated.append((kind, data.get("id")))
        return data


def seal(plain, kind, item_id, version, key=KEY):
    # The recorder's own encoder, so the fixture carries the exact snapshot envelope.
    return postgres_transfer._encode(ContentCipher(key).encrypt(plain, kind, item_id, version))


def ledger(value, domain, key=KEY):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return ledger_raw(raw, domain, key)


def ledger_raw(raw, domain, key=KEY):
    ciphertext = ContentCipher(key).encrypt(raw, "ledger", domain, "v1")
    return "enc1:" + base64.b64encode(ciphertext).decode("ascii")


def document(record, kind, item_id, key=KEY):
    """A content record exactly as ``PostgresStore._write_raw`` stores it."""
    raw = yaml.safe_dump(record, allow_unicode=True, sort_keys=False).encode("utf-8")
    version = hashlib.sha256(raw).hexdigest()
    return version, seal(raw, kind, item_id, version, key)


def block(columns, rows=()):
    return {"columns": list(columns), "rows": [[row[name] for name in columns] for row in rows]}


def snapshot(blocks=None, extra_tables=()):
    # A real export always carries every column; registered tables keep theirs with no rows.
    tables = {table: block(sorted(NEEDED.get(table, ())))
              for table in (*TABLES, *sorted(set(extra_tables)))}
    tables.update(blocks or {})
    return {"format": 1, "migrations": [], "tables": tables}


MEMORY = {"id": "m1", "content": "喜欢实际案例"}
LEGACY_HISTORY = {"id": "m1", "title": "旧版资料", "affinity": 40}
IMAGE = b"\x89PNG\r\n\x1a\n normalized input image"
ORIGINAL = b"qq original attachment"
STICKER = b"GIF89a shared sticker"
DOCUMENT = b"%PDF-1.4 file library entry"
ASSET = b"\x89PNG\r\n\x1a\n registered asset"


def full_snapshot(key=KEY):
    """One row for every registered payload domain, plus a cleaned media original."""
    memory_version, memory_body = document(MEMORY, "memories", "m1", key)
    config_version, config_body = document({"project_id": "p1"}, "manifest", "@config", key)
    history_version, history_body = document(LEGACY_HISTORY, "memories", "m1", key)
    image_sha = hashlib.sha256(IMAGE).hexdigest()
    original_sha = hashlib.sha256(ORIGINAL).hexdigest()
    cleaned_sha = hashlib.sha256(b"expired original").hexdigest()
    sticker_sha = hashlib.sha256(STICKER).hexdigest()
    document_sha = hashlib.sha256(DOCUMENT).hexdigest()
    asset_sha = hashlib.sha256(ASSET).hexdigest()
    return snapshot({
        "content_records": block(("kind", "id", "version", "revision", "body", "locked"), [
            {"kind": "memories", "id": "m1", "version": memory_version, "revision": 1,
             "body": memory_body, "locked": False},
            {"kind": "manifest", "id": "@config", "version": config_version, "revision": 1,
             "body": config_body, "locked": False},
        ]),
        "content_history": block(("kind", "id", "version", "body"), [
            {"kind": "memories", "id": "m1", "version": history_version, "body": history_body},
        ]),
        "state": block(("key", "value"), [
            {"key": "run", "value": ledger({"status": "paused"}, "state:run", key)},
            {"key": "relationship:abcd", "value": ledger(True, "state:relationship:abcd", key)},
        ]),
        "events": block(("account", "id", "body"), [
            {"account": "bili", "id": "e1", "body": ledger({"id": "e1", "kind": "comment"}, "events.body", key)},
        ]),
        "actions": block(("id", "payload", "result"), [
            {"id": "a1", "payload": ledger({"event_id": "e1"}, "actions.payload", key),
             "result": ledger({"rpid": "1"}, "actions.result", key)},
            {"id": "a2", "payload": ledger({"event_id": "e2"}, "actions.payload", key), "result": None},
        ]),
        "core_events": block(("id", "body", "version"), [
            {"id": "c1", "version": "v1",
             "body": ledger({"id": "c1", "body": "消息"}, "core_events.body", key)},
        ]),
        "core_actions": block(("id", "body", "result"), [
            {"id": "ca1", "body": ledger({"event": {"id": "c1"}}, "core_actions.body", key), "result": None},
            {"id": "ca2", "body": ledger({"event": {"id": "c2"}}, "core_actions.body", key),
             "result": ledger({"ok": True}, "core_actions.result", key)},
        ]),
        "actor_nodes": block(("actor_key", "identity_body"), [
            {"actor_key": "actor:local:1",
             "identity_body": ledger({"platform": "local", "user_id": "u1"}, "actor_nodes.identity", key)},
        ]),
        "relationship_claims": block(("id", "body"), [
            {"id": "rc1", "body": ledger({"relation": "friend", "direction": "subject_to_object"},
                                        "relationship_claims.body", key)},
        ]),
        "input_image_blobs": block(("id", "version", "body"), [
            {"id": "i" * 32, "version": image_sha,
             "body": seal(IMAGE, "input_images", "i" * 32, image_sha, key)},
        ]),
        "media_originals": block(("id", "sha256", "size", "metadata", "body", "status"), [
            {"id": "media-1", "sha256": original_sha, "size": len(ORIGINAL), "status": "available",
             "metadata": ledger({"kind": "image", "name": "a.png", "preserve": []},
                                "media-original:media-1", key),
             "body": seal(ORIGINAL, "media-original", "media-1", original_sha, key)},
            {"id": "media-2", "sha256": cleaned_sha, "size": len(b"expired original"),
             "status": "expired", "body": None,
             "metadata": ledger({"kind": "file", "name": "b.bin", "preserve": [],
                                 "cleanup_reason": "retention"}, "media-original:media-2", key)},
        ]),
        "sticker_library": block(("id", "sha256", "size", "metadata", "body"), [
            {"id": "st1", "sha256": sticker_sha, "size": len(STICKER),
             "metadata": ledger({"name": "笑", "tags": ["笑"]}, "sticker:st1", key),
             "body": seal(STICKER, "stickers", "st1", sticker_sha, key)},
        ]),
        "file_library": block(("id", "sha256", "size", "metadata", "body"), [
            {"id": "fl1", "sha256": document_sha, "size": len(DOCUMENT),
             "metadata": ledger({"name": "报告.pdf", "tags": [], "scopes": []}, "file-library:fl1", key),
             "body": seal(DOCUMENT, "file-library", "fl1", document_sha, key)},
        ]),
        "asset_restore_jobs": block(("id", "path", "digest", "body"), [
            {"id": "ar1", "path": "assets/persona/a.png", "digest": asset_sha,
             "body": seal(ASSET, "asset-restore", "ar1", asset_sha, key)},
        ]),
        "library_version_bodies": block(("kind", "id", "sha256", "size", "body"), [
            {"kind": "file-library", "id": "fl1", "sha256": document_sha, "size": len(DOCUMENT),
             "body": seal(DOCUMENT, "file-library", "fl1", document_sha, key)},
        ]),
        "library_versions": block(("revision", "metadata"), [
            {"revision": "v1", "metadata": ledger({"data": {"name": "旧版资料"}},
                                                 "library-version:v1", key)},
        ]),
    })


def cell(snapshot, path, row=0):
    """The mutable cell of one registered payload, plus its table block and position."""
    table, _, column = path.partition(".")
    block_ = snapshot["tables"][table]
    position = block_["columns"].index(column)
    return block_["rows"][row][position]


def set_cell(snapshot, path, value, row=0):
    table, _, column = path.partition(".")
    block_ = snapshot["tables"][table]
    block_["rows"][row][block_["columns"].index(column)] = value


def first_ciphertext(snapshot, path):
    table, _, column = path.partition(".")
    block_ = snapshot["tables"][table]
    position = block_["columns"].index(column)
    for row in block_["rows"]:
        if row[position] is not None:
            return row, position
    raise AssertionError("no ciphertext for " + path)


def encoded(value):
    return value["$bytes"] if isinstance(value, dict) else value[len("enc1:"):]


def reseal(value, raw):
    text = base64.b64encode(raw).decode("ascii")
    return {"$bytes": text} if isinstance(value, dict) else "enc1:" + text


def tamper(snapshot, path):
    """Flip one authentication-tag byte of the first non-null value of a registered field."""
    row, position = first_ciphertext(snapshot, path)
    raw = bytearray(base64.b64decode(encoded(row[position]), validate=True))
    raw[-1] ^= 1
    row[position] = reseal(row[position], bytes(raw))


def failure(snapshot, key=KEY, profile=None):
    with pytest.raises(AssistantError) as error:
        validate_snapshot(snapshot, key, profile or Profile())
    return error.value


# --- coverage of every registered domain --------------------------------------------------

def test_full_snapshot_verifies_every_registered_payload():
    result = validate_snapshot(full_snapshot(), KEY, Profile())
    assert result["payloads"] == {
        "content_records.body": 2,
        "content_history.body": 1,
        "state.value": 2,
        "events.body": 1,
        "actions.payload": 2,
        "actions.result": 1,
        "core_events.body": 1,
        "core_actions.body": 2,
        "core_actions.result": 1,
        "actor_nodes.identity_body": 1,
        "relationship_claims.body": 1,
        "input_image_blobs.body": 1,
        "media_originals.metadata": 2,
        "media_originals.body": 1,
        "sticker_library.metadata": 1,
        "sticker_library.body": 1,
        "file_library.metadata": 1,
        "file_library.body": 1,
        "library_version_bodies.body": 1,
        "library_versions.metadata": 1,
        "asset_restore_jobs.body": 1,
    }
    assert result["payloads_total"] == 26
    assert result["ciphertexts"] == 9
    assert result["ledger"] == 17
    assert result["legacy_ledger"] == 0
    assert "preset_reply_turns" in TABLES
    assert result["tables"] == len(TABLES) == 38
    assert result["encrypted_tables"] == 16
    assert result["plain_tables"] == 22
    assert result["rows"] == 21
    assert result["current_records"] == 2
    assert result["history_records"] == 1
    assert result["null_payloads"] == 3
    assert result["cleaned_media"] == 1


def test_result_reports_counts_only():
    result = validate_snapshot(full_snapshot(), KEY, Profile())
    assert set(result) == {"tables", "rows", "encrypted_tables", "plain_tables", "ciphertexts",
                           "ledger", "legacy_ledger", "current_records", "history_records",
                           "null_payloads", "cleaned_media", "payloads", "payloads_total"}
    scalars = [value for name, value in result.items() if name != "payloads"]
    assert all(type(value) is int for value in scalars)
    assert all(type(value) is int for value in result["payloads"].values())
    assert "案例" not in json.dumps(result, ensure_ascii=False)
    assert KEY.hex() not in json.dumps(result, ensure_ascii=False)


def test_validation_does_not_mutate_the_snapshot():
    synthetic = full_snapshot()
    before = copy.deepcopy(synthetic)
    validate_snapshot(synthetic, KEY, Profile())
    assert synthetic == before


def test_snapshot_survives_the_json_based_unpack_shape():
    # unpack returns json.loads of the snapshot, so only plain JSON values may be assumed.
    restored = json.loads(json.dumps(full_snapshot()))
    assert validate_snapshot(restored, KEY, Profile())["payloads_total"] == 26


def test_cleaned_media_body_and_optional_results_are_allowed():
    synthetic = snapshot({
        "media_originals": block(("id", "sha256", "size", "metadata", "body", "status"), [
            {"id": "media-9", "sha256": "c" * 64, "size": 0, "status": "expired", "body": None,
             "metadata": ledger({"kind": "file", "name": "b.bin", "preserve": []},
                                "media-original:media-9")},
        ]),
        "core_actions": block(("id", "body", "result"), [
            {"id": "ca1", "body": ledger({"event": {"id": "c1"}}, "core_actions.body"), "result": None},
        ]),
    })
    result = validate_snapshot(synthetic, KEY, Profile())
    assert result["cleaned_media"] == 1
    assert result["null_payloads"] == 2
    assert result["payloads"]["media_originals.metadata"] == 1
    assert result["payloads"]["media_originals.body"] == 0
    assert result["payloads"]["core_actions.result"] == 0
    # The cleaned row's metadata is still a registered payload and still verified.
    tamper(synthetic, "media_originals.metadata")
    assert failure(synthetic).code == "snapshot_integrity"


# --- current records versus old history ---------------------------------------------------

def test_current_record_uses_profile_and_history_only_decodes():
    profile = Profile()
    result = validate_snapshot(full_snapshot(), KEY, profile)
    # LEGACY_HISTORY lacks the currently required "content" field: it must not be rejected.
    # Config records are stored under "@config" and legitimately carry no "id" field.
    assert profile.validated == [("memories", "m1"), ("manifest", None)]
    assert result["current_records"] == 2 and result["history_records"] == 1


def test_current_record_rejected_by_profile():
    synthetic = full_snapshot()
    version, body = document({"id": "m1", "title": "旧版资料"}, "memories", "m1")
    row = synthetic["tables"]["content_records"]["rows"][0]
    row[2], row[4] = version, body
    assert failure(synthetic).code == "snapshot_content"


def test_unknown_kind_is_reported_without_echoing_it():
    synthetic = full_snapshot()
    version, body = document({"id": "m1", "content": "x"}, "retired_kind", "m1")
    row = synthetic["tables"]["content_records"]["rows"][0]
    row[0], row[2], row[4] = "retired_kind", version, body
    error = failure(synthetic)
    assert error.code == "snapshot_content"
    assert "retired_kind" not in str(error)


def test_record_id_must_match_its_row():
    synthetic = full_snapshot()
    version, body = document({"id": "m2", "content": "x"}, "memories", "m1")
    row = synthetic["tables"]["content_records"]["rows"][0]
    row[2], row[4] = version, body
    assert failure(synthetic).code == "snapshot_content"


def test_history_must_still_be_a_yaml_mapping():
    synthetic = full_snapshot()
    raw = yaml.safe_dump(["not", "a", "mapping"]).encode("utf-8")
    version = hashlib.sha256(raw).hexdigest()
    row = synthetic["tables"]["content_history"]["rows"][0]
    row[2], row[3] = version, seal(raw, "memories", "m1", version)
    assert failure(synthetic).code == "snapshot_content"


def test_history_must_still_be_utf8():
    synthetic = full_snapshot()
    raw = b"\xff\xfe not utf-8"
    version = hashlib.sha256(raw).hexdigest()
    row = synthetic["tables"]["content_history"]["rows"][0]
    row[2], row[3] = version, seal(raw, "memories", "m1", version)
    assert failure(synthetic).code == "snapshot_content"


# --- key and authenticity -----------------------------------------------------------------

@pytest.mark.parametrize("bad_key", [b"", bytes(31), bytes(33), "x" * 32, None])
def test_key_must_be_32_bytes(bad_key):
    assert failure(full_snapshot(), bad_key).code == "snapshot_key"


def test_wrong_key_is_rejected():
    assert failure(full_snapshot(), WRONG_KEY).code == "snapshot_integrity"


@pytest.mark.parametrize("path", REGISTERED)
def test_every_registered_ciphertext_is_rejected_when_tampered(path):
    synthetic = full_snapshot()
    tamper(synthetic, path)
    assert failure(synthetic).code == "snapshot_integrity"


def test_aad_kind_swap_between_media_and_stickers_is_rejected():
    item = "shared-id"
    raw = b"identical bytes"
    version = hashlib.sha256(raw).hexdigest()
    synthetic = snapshot({
        "media_originals": block(("id", "sha256", "size", "metadata", "body"), [
            {"id": item, "sha256": version, "size": len(raw), "body": seal(raw, "media-original", item, version),
             "metadata": ledger({"kind": "image"}, "media-original:" + item)}]),
        "sticker_library": block(("id", "sha256", "size", "metadata", "body"), [
            {"id": item, "sha256": version, "size": len(raw), "body": seal(raw, "stickers", item, version),
             "metadata": ledger({"name": "笑"}, "sticker:" + item)}]),
    })
    assert validate_snapshot(synthetic, KEY, Profile())["ciphertexts"] == 2
    # Same id, same version: only the registered AAD kind separates the two bodies.
    set_cell(synthetic, "sticker_library.body", cell(synthetic, "media_originals.body"))
    assert failure(synthetic).code == "snapshot_integrity"
    set_cell(synthetic, "sticker_library.body", seal(raw, "stickers", item, version))
    set_cell(synthetic, "media_originals.metadata", cell(synthetic, "sticker_library.metadata"))
    assert failure(synthetic).code == "snapshot_integrity"


def test_aad_id_swap_between_rows_of_one_table_is_rejected():
    synthetic = full_snapshot()
    first = cell(synthetic, "media_originals.metadata", 0)
    second = cell(synthetic, "media_originals.metadata", 1)
    set_cell(synthetic, "media_originals.metadata", second, 0)
    set_cell(synthetic, "media_originals.metadata", first, 1)
    assert failure(synthetic).code == "snapshot_integrity"


def test_aad_content_row_swap_is_rejected():
    synthetic = full_snapshot()
    rows = synthetic["tables"]["content_records"]["rows"]
    rows[0][4], rows[1][4] = rows[1][4], rows[0][4]
    assert failure(synthetic).code == "snapshot_integrity"


def test_ledger_domain_swap_is_rejected():
    synthetic = snapshot({
        "events": block(("id", "body"), [
            {"id": "e1", "body": ledger({"id": "e1"}, "events.body")}]),
        "core_events": block(("id", "body"), [
            # Same plaintext, different registered domain.
            {"id": "e1", "body": ledger({"id": "e1"}, "core_events.body")}]),
    })
    assert validate_snapshot(synthetic, KEY, Profile())["ledger"] == 2
    set_cell(synthetic, "core_events.body", cell(synthetic, "events.body"))
    assert failure(synthetic).code == "snapshot_integrity"


def test_state_domain_is_bound_to_the_row_key():
    synthetic = full_snapshot()
    first = cell(synthetic, "state.value", 0)
    second = cell(synthetic, "state.value", 1)
    set_cell(synthetic, "state.value", second, 0)
    set_cell(synthetic, "state.value", first, 1)
    assert failure(synthetic).code == "snapshot_integrity"


def test_media_body_digest_and_size_are_verified():
    raw = b"original attachment"
    version = hashlib.sha256(raw).hexdigest()
    synthetic = snapshot({
        "media_originals": block(("id", "sha256", "size", "metadata", "body"), [
            {"id": "media-1", "sha256": version, "size": len(raw),
             "metadata": ledger({"kind": "image"}, "media-original:media-1"),
             "body": seal(raw, "media-original", "media-1", version)}]),
    })
    assert validate_snapshot(synthetic, KEY, Profile())["ciphertexts"] == 1
    set_cell(synthetic, "media_originals.size", len(raw) + 1)
    assert failure(synthetic).code == "snapshot_integrity"


def test_content_version_must_be_the_plaintext_digest():
    raw = b"id: m1\ncontent: x\n"
    synthetic = snapshot({
        "content_records": block(("kind", "id", "version", "body"), [
            # The AAD version matches the column, but it is not the digest of the plaintext.
            {"kind": "memories", "id": "m1", "version": "a" * 64,
             "body": seal(raw, "memories", "m1", "a" * 64)}]),
    })
    assert failure(synthetic).code == "snapshot_integrity"


# --- envelope, base64 and ledger strictness ----------------------------------------------

def test_bytea_envelope_must_be_exactly_bytes():
    synthetic = full_snapshot()
    value = cell(synthetic, "content_records.body")
    set_cell(synthetic, "content_records.body", {**value, "extra": 1})
    assert failure(synthetic).code == "snapshot_structure"


def test_bytea_column_must_not_hold_plain_text():
    synthetic = full_snapshot()
    set_cell(synthetic, "content_records.body", "not an envelope")
    assert failure(synthetic).code == "snapshot_structure"


@pytest.mark.parametrize("path, broken", [
    ("content_records.body", {"$bytes": "not base64!!"}),
    ("state.value", "enc1:not base64!!"),
    ("state.value", "enc1:"),
])
def test_invalid_base64_is_rejected(path, broken):
    synthetic = full_snapshot()
    set_cell(synthetic, path, broken)
    assert failure(synthetic).code == "snapshot_integrity"


def test_ledger_column_must_carry_an_encrypted_payload():
    synthetic = full_snapshot()
    set_cell(synthetic, "state.value", json.dumps({"status": "paused"}))
    assert failure(synthetic).code == "snapshot_integrity"


def test_ledger_payload_must_be_json():
    synthetic = full_snapshot()
    set_cell(synthetic, "state.value", ledger_raw(b"\xff\xfe", "state:run"))
    assert failure(synthetic).code == "snapshot_content"


def test_ledger_payload_must_not_contain_nan():
    synthetic = full_snapshot()
    set_cell(synthetic, "state.value", ledger_raw(b'{"a": NaN}', "state:run"))
    assert failure(synthetic).code == "snapshot_content"


def test_ledger_mapping_domains_require_a_json_object():
    synthetic = full_snapshot()
    set_cell(synthetic, "events.body", ledger([1, 2], "events.body"))
    assert failure(synthetic).code == "snapshot_content"


def test_legacy_journal_rows_are_verified_when_encrypted():
    synthetic = full_snapshot()
    synthetic["tables"]["relationship_pending"] = block(("receipt", "data", "after_yaml"), [
        {"receipt": "relationship:x",
         "data": ledger({"id": "u1"}, "relationship_pending.data"),
         "after_yaml": ledger("id: u1\n", "relationship_pending.after_yaml")}])
    assert validate_snapshot(synthetic, KEY, Profile())["legacy_ledger"] == 2
    tamper(synthetic, "relationship_pending.data")
    assert failure(synthetic).code == "snapshot_integrity"


def test_legacy_journal_rows_need_their_registered_columns():
    synthetic = full_snapshot()
    synthetic["tables"]["relationship_pending"] = block(("receipt",), [
        {"receipt": "relationship:x"}])
    assert failure(synthetic).code == "snapshot_schema"


def test_legacy_journal_plain_text_is_rejected():
    synthetic = full_snapshot()
    synthetic["tables"]["relationship_pending"] = block(("receipt", "data", "after_yaml"), [
        {"receipt": "relationship:x", "data": json.dumps({"id": "u1"}), "after_yaml": "id: u1\n"}])
    assert failure(synthetic).code == "snapshot_integrity"


# --- snapshot structure and schema -------------------------------------------------------

def test_snapshot_envelope_is_strict():
    for broken in ({"migrations": [], "tables": {}}, {"format": 2, "tables": {}},
                   {"format": 1, "tables": []}, {"format": 1}):
        assert failure(broken).code == "snapshot_structure"


def test_table_block_must_hold_unique_columns_and_matching_rows():
    synthetic = full_snapshot()
    synthetic["tables"]["sessions"] = []
    assert failure(synthetic).code == "snapshot_structure"
    synthetic = full_snapshot()
    synthetic["tables"]["sessions"] = {"columns": ["thread"], "rows": []}
    synthetic["tables"]["sessions"]["columns"] = ["thread", "thread"]
    assert failure(synthetic).code == "snapshot_structure"
    synthetic = full_snapshot()
    synthetic["tables"]["sessions"] = {"columns": ["thread"], "rows": [["t1", "extra"]]}
    assert failure(synthetic).code == "snapshot_structure"


def test_missing_registered_column_is_rejected():
    synthetic = full_snapshot()
    block_ = synthetic["tables"]["content_records"]
    position = block_["columns"].index("body")
    del block_["columns"][position]
    for row in block_["rows"]:
        del row[position]
    assert failure(synthetic).code == "snapshot_schema"


def test_table_set_must_match_the_current_release():
    synthetic = full_snapshot()
    del synthetic["tables"]["governance_grants"]
    assert failure(synthetic).code == "snapshot_schema"
    synthetic = full_snapshot()
    synthetic["tables"]["unknown_table"] = block(())
    assert failure(synthetic).code == "snapshot_schema"


def test_profile_extra_tables_take_part_in_the_table_set():
    with_extra = snapshot(extra_tables=("videos",))
    result = validate_snapshot(with_extra, KEY, Profile({"videos"}))
    assert result["tables"] == len(TABLES) + 1
    assert result["plain_tables"] == 23
    assert failure(with_extra, profile=Profile()).code == "snapshot_schema"
    assert failure(snapshot(), profile=Profile({"videos"})).code == "snapshot_schema"


# --- error surface -----------------------------------------------------------------------

def test_error_messages_never_carry_payload_text_or_the_key():
    marker = "SECRET-正文-标记"
    synthetic = full_snapshot()
    version, body = document({"id": "m1", "title": marker}, "memories", "m1")
    row = synthetic["tables"]["content_records"]["rows"][0]
    row[2], row[4] = version, body
    error = failure(synthetic)
    assert error.code == "snapshot_content"
    assert str(error) == "快照解密内容无效"
    assert marker not in str(error) and KEY.hex() not in str(error)


def test_every_failure_uses_a_fixed_code_and_message():
    cases = []
    synthetic = full_snapshot()
    tamper(synthetic, "state.value")
    cases.append((synthetic, KEY))
    cases.append((full_snapshot(), WRONG_KEY))
    cases.append((snapshot(extra_tables=("videos",)), KEY))
    for synthetic, key in cases:
        error = failure(synthetic, key)
        assert error.code.startswith("snapshot_")
        assert str(error).endswith(("无效", "失败", "不一致"))
        assert "enc1:" not in str(error)


# --- maintainability: the registry must follow the SQL schema ----------------------------

NOT_A_COLUMN = {"check", "primary", "unique", "foreign", "constraint", "references", "exclude"}
CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)\s*\(", re.I)
ALTER_TABLE = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([a-z_][a-z0-9_]*)([^;]*);", re.I)
COLUMN = re.compile(r"([a-z_][a-z0-9_]*)", re.I)
BYTEA = re.compile(r"\bBYTEA\b", re.I)
ENC1 = re.compile(r"left\(\s*([a-z_][a-z0-9_]*)\s*,\s*5\s*\)\s*=\s*'enc1:'", re.I)


def balanced(text, start):
    """The parenthesized body that starts at ``text[start] == '('``."""
    depth = 0
    for position in range(start, len(text)):
        if text[position] == "(":
            depth += 1
        elif text[position] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:position]
    raise AssertionError("unbalanced parentheses in SQL")


def definitions(body):
    """Top-level comma-separated column and constraint definitions of one table body."""
    parts, depth, start, quoted = [], 0, 0, False
    for position, character in enumerate(body):
        if character == "'":
            quoted = not quoted
        elif quoted:
            continue
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(body[start:position])
            start = position + 1
    parts.append(body[start:])
    return [part.strip() for part in parts if part.strip()]


def sql_encrypted_columns(documents=None):
    """Every BYTEA and ``left(column,5)='enc1:'`` column of the installed migrations."""
    if documents is None:
        documents = [(path.name, path.read_text(encoding="utf-8"))
                     for path in sorted(SQL.glob("*.sql"))]
    bytea, enc1 = set(), set()
    for _, text in documents:
        for match in CREATE_TABLE.finditer(text):
            body = balanced(text, match.end() - 1)
            for definition in definitions(body):
                name = COLUMN.match(definition)
                if name and name.group(1).lower() not in NOT_A_COLUMN and BYTEA.search(definition):
                    bytea.add((match.group(1), name.group(1)))
            enc1.update((match.group(1), column) for column in ENC1.findall(body))
        for match in ALTER_TABLE.finditer(text):
            enc1.update((match.group(1), column) for column in ENC1.findall(match.group(2)))
    return bytea, enc1


def test_registry_covers_every_encrypted_column_of_the_current_sql():
    bytea, enc1 = sql_encrypted_columns()
    assert len(bytea) == 8 and len(enc1) == 13
    assert bytea == {(field.table, field.column) for field in CIPHER_PAYLOADS}
    assert enc1 == {(field.table, field.column) for field in LEDGER_PAYLOADS}


def test_detector_reacts_to_a_newly_encrypted_column():
    # Guards this very test: a BYTEA column or a new enc1 CHECK must reach the comparison above.
    synthetic = """
    CREATE TABLE new_payloads(
     id TEXT PRIMARY KEY, body BYTEA NOT NULL,
     metadata TEXT NOT NULL CHECK(left(metadata,5)='enc1:'),
     kept TEXT NOT NULL, CHECK(left(kept,5)='enc1:'));
    ALTER TABLE media_originals ADD CONSTRAINT preview_encrypted CHECK(left(preview,5)='enc1:');
    ALTER TABLE conversation_index ALTER COLUMN sequence TYPE BIGINT;
    """
    bytea, enc1 = sql_encrypted_columns([("999_synthetic.sql", synthetic)])
    assert bytea == {("new_payloads", "body")}
    assert enc1 == {("new_payloads", "metadata"), ("new_payloads", "kept"),
                    ("media_originals", "preview")}
    assert (bytea | enc1) - {(field.table, field.column)
                             for field in (*CIPHER_PAYLOADS, *LEDGER_PAYLOADS)}


def test_registry_tables_are_transfer_tables():
    for field in (*CIPHER_PAYLOADS, *LEDGER_PAYLOADS, *LEGACY_LEDGER_PAYLOADS):
        assert field.table in TABLES
    assert {(field.table, field.column) for field in CIPHER_PAYLOADS} & \
           {(field.table, field.column) for field in LEDGER_PAYLOADS} == set()


def test_ledger_domains_match_the_legacy_import_encoding():
    registered = {(field.table, field.column) for field in LEGACY_LEDGER_PAYLOADS}
    literal = {(field.table, field.column) for field in LEDGER_PAYLOADS}
    expected = {(table, column) for table, columns in legacy_import.ENCRYPTED.items()
                for column in columns}
    # legacy_import re-encodes each of these columns as ledger_encode(table + "." + column).
    assert registered <= expected
    assert expected - registered == literal & expected
    for field in LEGACY_LEDGER_PAYLOADS:
        assert field.domain == field.table + "." + field.column
    for field in LEDGER_PAYLOADS:
        if (field.table, field.column) in expected:
            assert field.domain == field.table + "." + field.column
