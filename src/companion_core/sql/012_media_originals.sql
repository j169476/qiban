-- Content and descriptive metadata are encrypted. Routing/retention fields
-- contain only opaque references, byte counts and lifecycle state.
CREATE TABLE media_originals (
 id TEXT PRIMARY KEY,
 scope TEXT NOT NULL,
 event_id TEXT NOT NULL REFERENCES core_events(id),
 source_version TEXT NOT NULL,
 sha256 TEXT NOT NULL,
 size BIGINT NOT NULL CHECK(size BETWEEN 0 AND 10485760),
 metadata TEXT NOT NULL CHECK(left(metadata,5)='enc1:'),
 body BYTEA,
 revision TEXT NOT NULL,
 created_at DOUBLE PRECISION NOT NULL,
 processed_at DOUBLE PRECISION,
 preserved BOOLEAN NOT NULL DEFAULT FALSE,
 status TEXT NOT NULL CHECK(status IN ('available','expired')),
 CHECK ((status='available')=(body IS NOT NULL))
);
CREATE INDEX media_originals_expiry ON media_originals(processed_at,id) WHERE status='available' AND NOT preserved;
CREATE TABLE media_original_holds (
 owner TEXT NOT NULL,
 task TEXT NOT NULL,
 asset TEXT NOT NULL REFERENCES media_originals(id),
 PRIMARY KEY(owner,task,asset)
);
