"""Pure in-memory integrity validation of one portable application snapshot.

`postgres_transfer.unpack` returns the decrypted snapshot structure whose payloads are
still encrypted. This module verifies every registered encrypted payload of such a
snapshot against the database data key that produced it: no database connection, no
network, no credential access, no file writes, and no plaintext or key material in the
returned counters or in the raised error messages.

Two encryption boundaries of this application are registered explicitly, taken from the
real write sites and from the installed SQL schema:

* ``ContentCipher`` on ``BYTEA`` columns: associated data is
  ``["companion-content-v1", kind, item_id, version]`` built from the row's own columns
  (``cipher.py``; ``postgres.py`` uses ``(kind, id or "@config", version)``).
* ``PostgresStore.ledger_encode`` on ``enc1:`` text columns: associated data is
  ``["companion-content-v1", "ledger", domain, "v1"]`` where the domain is either a
  literal (``core_events.body``) or derived from the row (``state:<key>``,
  ``media-original:<id>``...); see ``postgres.py``.

Deliberately **not** verified here, and left to the staged restore that runs against a
real database: SQL constraints and CHECKs, owners/grants/provenance/ACL rows, table
column equality with the live database, foreign keys and cross-table references, image
and file formats or pixel bounds, on-disk assets, whether a restore job is still pending
(its payload is still verified when such a row exists), and schema migration
compatibility. A successful result here means "every encrypted payload is authentic for
its registered AAD and its decrypted form is usable"; it is not a proof that the database
contents satisfy the application schema.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import yaml

from .cipher import ContentCipher
from .errors import AssistantError
from .storage import StrictLoader, digest

SNAPSHOT_FORMAT = 1
LEDGER_PREFIX = "enc1:"
LEDGER_KIND = "ledger"
LEDGER_VERSION = "v1"
CONFIG_ID = "@config"

# Plaintext caps enforced by the writers; a mismatch means the payload is not genuine.
MAX_CONTENT_BYTES = 1_000_000  # postgres.PostgresStore._write_raw
MAX_LEDGER_BYTES = 2_000_000  # postgres.PostgresStore.ledger_encode
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # input_images.MAX_BYTES
MAX_LIBRARY_BYTES = 10 * 1024 * 1024  # media_originals/sticker_library/file_library MAX_BYTES
MAX_ASSET_BYTES = 10_000_000  # asset_transfer._validate

# Fixed, non-interpolated messages: no payload text, digest, table value or key can
# reach a caller through an exception.
_MESSAGES = {
    "snapshot_key": "快照校验密钥无效",
    "snapshot_structure": "快照结构无效，无法校验加密载荷",
    "snapshot_schema": "快照资料库结构与当前应用版本不一致",
    "snapshot_integrity": "快照密文完整性校验失败",
    "snapshot_content": "快照解密内容无效",
}


def _fail(code):
    raise AssistantError(code, _MESSAGES[code]) from None


@dataclass(frozen=True)
class CipherPayload:
    """A ``BYTEA`` column holding ``ContentCipher`` output for ``(kind, id, version)``."""

    table: str
    column: str
    id_column: str
    version_column: str
    kind: str = ""
    kind_column: str = ""
    document: str = ""  # "" opaque bytes, "current" record, "history" old record
    nullable: bool = False
    size_column: str = ""
    max_bytes: int = 0

    @property
    def path(self):
        return self.table + "." + self.column

    @property
    def needs(self):
        names = {self.column, self.id_column, self.version_column}
        names |= {name for name in (self.kind_column, self.size_column) if name}
        return frozenset(names)


@dataclass(frozen=True)
class LedgerPayload:
    """A text column holding ``enc1:`` output of ``PostgresStore.ledger_encode``."""

    table: str
    column: str
    domain: str = ""
    prefix: str = ""
    key_column: str = ""
    nullable: bool = False
    mapping: bool = False

    @property
    def path(self):
        return self.table + "." + self.column

    @property
    def needs(self):
        return frozenset({self.column, self.key_column} if self.key_column else {self.column})


# Every BYTEA/enc1 encrypted column of the current SQL schema, with the exact AAD used
# by the writer that fills it. The coverage test in tests/test_portable_validation.py
# parses the SQL migrations and fails when a column is missing here or added here only.
CIPHER_PAYLOADS = (
    # postgres.PostgresStore._write_raw: encrypt(raw, kind, id or "@config", version)
    CipherPayload("content_records", "body", "id", "version", kind_column="kind",
                  document="current", max_bytes=MAX_CONTENT_BYTES),
    # Same writer; archived rows keep their own kind/id/version.
    CipherPayload("content_history", "body", "id", "version", kind_column="kind",
                  document="history", max_bytes=MAX_CONTENT_BYTES),
    # input_images.InputImages.upload: encrypt(raw, "input_images", id, sha256(raw))
    CipherPayload("input_image_blobs", "body", "id", "version", kind="input_images",
                  max_bytes=MAX_IMAGE_BYTES),
    # media_originals.MediaOriginals.put; retention may clean the body (NULL) later.
    CipherPayload("media_originals", "body", "id", "sha256", kind="media-original",
                  nullable=True, size_column="size", max_bytes=MAX_LIBRARY_BYTES),
    # sticker_library.StickerLibrary.import_bytes
    CipherPayload("sticker_library", "body", "id", "sha256", kind="stickers",
                  size_column="size", max_bytes=MAX_LIBRARY_BYTES),
    # file_library.FileLibrary.import_bytes
    CipherPayload("file_library", "body", "id", "sha256", kind="file-library",
                  size_column="size", max_bytes=MAX_LIBRARY_BYTES),
    CipherPayload("library_version_bodies", "body", "id", "sha256", kind_column="kind",
                  size_column="size", max_bytes=MAX_LIBRARY_BYTES),
    # asset_transfer.enqueue/finish; a complete export has no pending job rows.
    CipherPayload("asset_restore_jobs", "body", "id", "digest", kind="asset-restore",
                  max_bytes=MAX_ASSET_BYTES),
)

# These writers encrypt bodies using the row ID. Their original migrations
# declare TEXT without an enc1 CHECK. Direct and staged restores verify them too.
ROLE_BODY_PAYLOADS = (
    LedgerPayload("role_reminders", "body", prefix="role-reminder:", key_column="id", mapping=True),
    LedgerPayload("role_matters", "body", prefix="matter:", key_column="id", mapping=True),
    LedgerPayload("privacy_slices", "body", prefix="privacy:", key_column="id", mapping=True),
)

LEDGER_PAYLOADS = (
    # postgres.PostgresStore.set_state: ledger_encode("state:" + key, value)
    LedgerPayload("state", "value", prefix="state:", key_column="key"),
    # bili_assistant engine / legacy import
    LedgerPayload("events", "body", domain="events.body", mapping=True),
    LedgerPayload("actions", "payload", domain="actions.payload", mapping=True),
    LedgerPayload("actions", "result", domain="actions.result", nullable=True),
    # events.ingest and every core runtime ledger write
    LedgerPayload("core_events", "body", domain="core_events.body", mapping=True),
    LedgerPayload("core_actions", "body", domain="core_actions.body", mapping=True),
    LedgerPayload("core_actions", "result", domain="core_actions.result", nullable=True),
    # relations.register_actor: ledger_encode("actor_nodes.identity", actor.as_dict())
    LedgerPayload("actor_nodes", "identity_body", domain="actor_nodes.identity", mapping=True),
    # relations.propose
    LedgerPayload("relationship_claims", "body", domain="relationship_claims.body", mapping=True),
    # media_originals/sticker_library/file_library descriptive metadata
    LedgerPayload("media_originals", "metadata", prefix="media-original:", key_column="id",
                  mapping=True),
    LedgerPayload("sticker_library", "metadata", prefix="sticker:", key_column="id", mapping=True),
    LedgerPayload("file_library", "metadata", prefix="file-library:", key_column="id", mapping=True),
    LedgerPayload("library_versions", "metadata", prefix="library-version:", key_column="revision", mapping=True),
    *ROLE_BODY_PAYLOADS,
)

# The PostgreSQL backend never journals a pending relationship: relationship_store.commit
# returns early for it, and legacy_import re-encodes the SQLite-era journal with
# ledger_encode(table + "." + column, ...). Such rows therefore carry ``enc1:`` too, but
# no SQL CHECK declares it, so the SQL coverage test cannot discover these columns.
LEGACY_LEDGER_PAYLOADS = (
    LedgerPayload("relationship_pending", "data", domain="relationship_pending.data", mapping=True),
    LedgerPayload("relationship_pending", "after_yaml", domain="relationship_pending.after_yaml"),
)


def _group(entries):
    grouped = {}
    for entry in entries:
        grouped.setdefault(entry.table, []).append(entry)
    return {table: tuple(items) for table, items in grouped.items()}


_CIPHER_BY_TABLE = _group(CIPHER_PAYLOADS)
_LEDGER_BY_TABLE = _group(LEDGER_PAYLOADS)
_LEGACY_BY_TABLE = _group(LEGACY_LEDGER_PAYLOADS)


def _cipher(data_key):
    if not isinstance(data_key, bytes) or len(data_key) != 32:
        _fail("snapshot_key")
    try:
        return ContentCipher(data_key)
    except AssistantError:
        _fail("snapshot_key")


def _expected_tables(profile):
    # Imported lazily so postgres_transfer itself may import this module later.
    from .postgres_transfer import TABLES

    extra = set(getattr(profile, "extra_tables", ()) or ()) - set(TABLES)
    return (*TABLES, *sorted(extra))


def _text(value):
    if not isinstance(value, str) or not value:
        _fail("snapshot_structure")
    return value


def _envelope(value):
    """``postgres_transfer._encode`` stores BYTEA as exactly ``{"$bytes": base64}``."""
    if not isinstance(value, dict) or set(value) != {"$bytes"}:
        _fail("snapshot_structure")
    encoded = value["$bytes"]
    if not isinstance(encoded, str):
        _fail("snapshot_structure")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        _fail("snapshot_integrity")


def _document(plain):
    """Content bodies are UTF-8 YAML mappings, exactly as ``PostgresStore._decode`` reads them."""
    try:
        data = yaml.load(plain.decode("utf-8"), Loader=StrictLoader)
    except (ValueError, UnicodeError, yaml.YAMLError):
        _fail("snapshot_content")
    if not isinstance(data, dict):
        _fail("snapshot_content")
    return data


def _reject_constant(value):
    # ledger_encode uses allow_nan=False, so a well-formed ledger never contains these.
    raise ValueError(value)


def _json(plain):
    try:
        return json.loads(plain, parse_constant=_reject_constant)
    except (ValueError, UnicodeError):
        _fail("snapshot_content")


def _block(block):
    if not isinstance(block, dict):
        _fail("snapshot_structure")
    columns, rows = block.get("columns"), block.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        _fail("snapshot_structure")
    seen = set()
    for name in columns:
        if not isinstance(name, str) or not name or name in seen:
            _fail("snapshot_structure")
        seen.add(name)
    width = len(columns)
    for row in rows:
        if not isinstance(row, list) or len(row) != width:
            _fail("snapshot_structure")
    return columns, rows


def _table_blocks(snapshot, expected):
    if not isinstance(snapshot, dict) or snapshot.get("format") != SNAPSHOT_FORMAT:
        _fail("snapshot_structure")
    tables = snapshot.get("tables")
    if not isinstance(tables, dict):
        _fail("snapshot_structure")
    if set(tables) != set(expected):
        _fail("snapshot_schema")
    return {table: _block(tables[table]) for table in expected}


class _Checker:
    """Counters plus the two payload kinds; plaintext never leaves this object."""

    def __init__(self, cipher, profile):
        self.cipher, self.profile = cipher, profile
        self.counts = {field.path: 0 for field in (*CIPHER_PAYLOADS, *LEDGER_PAYLOADS)}
        self.cipher_count = self.ledger_count = self.legacy_count = 0
        self.current = self.history = self.null = self.cleaned = 0

    def plaintext(self, ciphertext, kind, item_id, version):
        # Untrusted bytes must never surface a library-specific exception type.
        try:
            return self.cipher.decrypt(ciphertext, kind, item_id, version)
        except Exception:
            _fail("snapshot_integrity")

    def cipher_field(self, field, row, index):
        value = row[index[field.column]]
        if value is None:
            if not field.nullable:
                _fail("snapshot_structure")
            self.null += 1
            if field.table == "media_originals" and field.column == "body":
                self.cleaned += 1
            return
        kind = _text(row[index[field.kind_column]]) if field.kind_column else field.kind
        item_id = _text(row[index[field.id_column]])
        version = _text(row[index[field.version_column]])
        plain = self.plaintext(_envelope(value), kind, item_id, version)
        if digest(plain) != version:
            _fail("snapshot_integrity")
        if field.size_column:
            size = row[index[field.size_column]]
            if not isinstance(size, int) or isinstance(size, bool):
                _fail("snapshot_structure")
            if size != len(plain):
                _fail("snapshot_integrity")
        if field.max_bytes and not 0 < len(plain) <= field.max_bytes:
            _fail("snapshot_integrity")
        if field.document == "current":
            self.record(kind, item_id, _document(plain))
            self.current += 1
        elif field.document == "history":
            # Old versions must stay readable; the current schema must not reject them.
            _document(plain)
            self.history += 1
        self.counts[field.path] += 1
        self.cipher_count += 1

    def record(self, kind, item_id, data):
        try:
            self.profile.validate(kind, data)
        except Exception:
            # A profile-specific validator must not decide the error type of a snapshot.
            _fail("snapshot_content")
        if item_id != CONFIG_ID and data.get("id") != item_id:
            _fail("snapshot_content")

    def ledger_field(self, field, row, index):
        value = row[index[field.column]]
        if value is None:
            if not field.nullable:
                _fail("snapshot_structure")
            self.null += 1
            return
        domain = field.domain
        if field.key_column:
            domain = field.prefix + _text(row[index[field.key_column]])
        self.ledger_value(value, domain, field.mapping)
        self.counts[field.path] += 1
        self.ledger_count += 1

    def legacy_field(self, field, row, index):
        value = row[index[field.column]]
        if value is None:
            _fail("snapshot_structure")
        self.ledger_value(value, field.domain, field.mapping)
        self.legacy_count += 1

    def ledger_value(self, value, domain, mapping):
        if not isinstance(value, str) or not value.startswith(LEDGER_PREFIX):
            _fail("snapshot_integrity")
        try:
            ciphertext = base64.b64decode(value[len(LEDGER_PREFIX):], validate=True)
        except (ValueError, TypeError):
            _fail("snapshot_integrity")
        plain = self.plaintext(ciphertext, LEDGER_KIND, domain, LEDGER_VERSION)
        if not 0 < len(plain) <= MAX_LEDGER_BYTES:
            _fail("snapshot_integrity")
        data = _json(plain)
        if mapping and not isinstance(data, dict):
            _fail("snapshot_content")
        return data


def validate_snapshot(snapshot, data_key, profile):
    """Verify every registered encrypted payload of one ``postgres_transfer.unpack`` result.

    Returns counters only. The snapshot structure must match the current release: the
    table set is exactly ``postgres_transfer.TABLES`` plus ``profile.extra_tables``, and
    every registered encrypted column must be present with a well-formed row.
    """
    cipher = _cipher(data_key)
    expected = _expected_tables(profile)
    blocks = _table_blocks(snapshot, expected)
    checker = _Checker(cipher, profile)
    rows = encrypted = 0
    for table in expected:
        columns, block_rows = blocks[table]
        index = {name: position for position, name in enumerate(columns)}
        cipher_fields = _CIPHER_BY_TABLE.get(table, ())
        ledger_fields = _LEDGER_BY_TABLE.get(table, ())
        legacy_fields = _LEGACY_BY_TABLE.get(table, ())
        required = (*cipher_fields, *ledger_fields)
        if block_rows:
            # The legacy journal only holds encrypted columns when it has rows at all.
            required += legacy_fields
        for field in required:
            if not field.needs.issubset(index):
                _fail("snapshot_schema")
        if cipher_fields or ledger_fields:
            encrypted += 1
        rows += len(block_rows)
        for row in block_rows:
            for field in cipher_fields:
                checker.cipher_field(field, row, index)
            for field in ledger_fields:
                checker.ledger_field(field, row, index)
            for field in legacy_fields:
                checker.legacy_field(field, row, index)
    return {
        "tables": len(expected),
        "rows": rows,
        "encrypted_tables": encrypted,
        "plain_tables": len(expected) - encrypted,
        "ciphertexts": checker.cipher_count,
        "ledger": checker.ledger_count,
        "legacy_ledger": checker.legacy_count,
        "current_records": checker.current,
        "history_records": checker.history,
        "null_payloads": checker.null,
        "cleaned_media": checker.cleaned,
        "payloads": dict(checker.counts),
        "payloads_total": sum(checker.counts.values()),
    }
