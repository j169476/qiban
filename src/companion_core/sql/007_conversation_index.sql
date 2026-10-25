CREATE TABLE conversation_index (
 kind TEXT NOT NULL, id TEXT NOT NULL, account TEXT NOT NULL, thread_id TEXT NOT NULL,
 stamp DOUBLE PRECISION NOT NULL, sequence INTEGER NOT NULL, part INTEGER NOT NULL,
 PRIMARY KEY(kind,id), FOREIGN KEY(kind,id) REFERENCES content_records(kind,id) ON DELETE CASCADE
);
CREATE INDEX conversation_index_page ON conversation_index(kind,account,thread_id,stamp,id);
