CREATE TABLE personal_fact_index (
 kind TEXT NOT NULL CHECK (kind='memories'), id TEXT NOT NULL,
 scope TEXT NOT NULL, attribute TEXT NOT NULL, value_key TEXT NOT NULL,
 valid_from TEXT NOT NULL, supersedes TEXT NOT NULL, version TEXT NOT NULL,
 stamp DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(kind,id), FOREIGN KEY(kind,id) REFERENCES content_records(kind,id) ON DELETE CASCADE
);
CREATE INDEX personal_fact_scope_page ON personal_fact_index(scope,stamp DESC,id);
