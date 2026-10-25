-- Metadata-only historical evidence. Only the core registers virtual resources.
CREATE TABLE governance_provenance (
 resource TEXT PRIMARY KEY REFERENCES governance_policies(resource) ON DELETE CASCADE,
 target TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('scope','snapshot')),
 origin_version TEXT NOT NULL
);
CREATE INDEX governance_provenance_target ON governance_provenance(target);
