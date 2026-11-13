-- Queue references only; message bodies and window checkpoints remain encrypted.
CREATE TABLE IF NOT EXISTS qq_native_inbox (
    scope TEXT NOT NULL,
    event_id TEXT NOT NULL,
    position BIGINT NOT NULL,
    PRIMARY KEY (scope, event_id)
);
CREATE INDEX IF NOT EXISTS qq_native_inbox_order ON qq_native_inbox(scope, position);
