-- An existing item's restore may match another item's current bytes. Ordinary import
-- still deduplicates inside the PostgreSQL installation-wide writer transaction.
ALTER TABLE file_library DROP CONSTRAINT file_library_sha256_key;
ALTER TABLE sticker_library DROP CONSTRAINT sticker_library_sha256_key;
CREATE INDEX file_library_digest ON file_library(sha256,created_at,id);
CREATE INDEX sticker_library_digest ON sticker_library(sha256,created_at,id);

-- A version stores editable metadata only. Access grants always belong to the live item.
CREATE TABLE library_versions (
 kind TEXT NOT NULL CHECK(kind IN ('file-library','stickers')),
 id TEXT NOT NULL,
 position BIGINT NOT NULL CHECK(position > 0),
 revision TEXT NOT NULL UNIQUE,
 sha256 TEXT NOT NULL,
 size BIGINT NOT NULL CHECK(size BETWEEN 1 AND 10485760),
 metadata TEXT NOT NULL CHECK(left(metadata,5)='enc1:'),
 archived_at DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(kind,id,position)
);
-- Old revisions with identical original bytes share one encrypted payload.
CREATE TABLE library_version_bodies (
 kind TEXT NOT NULL CHECK(kind IN ('file-library','stickers')),
 id TEXT NOT NULL,
 sha256 TEXT NOT NULL,
 size BIGINT NOT NULL CHECK(size BETWEEN 1 AND 10485760),
 body BYTEA NOT NULL,
 PRIMARY KEY(kind,id,sha256)
);
