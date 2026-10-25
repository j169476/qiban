CREATE TABLE input_image_blobs (
 id TEXT PRIMARY KEY, version TEXT NOT NULL, body BYTEA NOT NULL,
 actor TEXT NOT NULL, conversation TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
 event_id TEXT, CHECK(octet_length(body)<=8389632)
);
CREATE INDEX input_image_owner ON input_image_blobs(actor,conversation,event_id,created_at);
CREATE TRIGGER input_image_change AFTER INSERT OR UPDATE OR DELETE ON input_image_blobs
 FOR EACH STATEMENT EXECUTE FUNCTION companion_data_changed();
