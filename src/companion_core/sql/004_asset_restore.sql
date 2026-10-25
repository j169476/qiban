CREATE TABLE asset_restore_jobs(
 id TEXT PRIMARY KEY,path TEXT NOT NULL,digest TEXT NOT NULL,body BYTEA NOT NULL,
 CHECK(length(digest)=64),CHECK(octet_length(body)<=11000000));
