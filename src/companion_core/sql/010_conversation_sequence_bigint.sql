-- Native platform message sequences (including Bilibili msg_seqno) exceed int32.
-- Preserve both the encrypted source record and the exact ordering projection.
ALTER TABLE conversation_index ALTER COLUMN sequence TYPE BIGINT;
