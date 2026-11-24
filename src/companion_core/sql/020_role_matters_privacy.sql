CREATE TABLE privacy_jobs (
 resource text PRIMARY KEY, source_version text NOT NULL, cursor_pos integer NOT NULL DEFAULT 0,
 due double precision NOT NULL DEFAULT 0, complete integer NOT NULL DEFAULT 0
);
CREATE INDEX privacy_jobs_due ON privacy_jobs(complete,due,resource);
CREATE TABLE privacy_slices (
 id text PRIMARY KEY, resource text NOT NULL, source_version text NOT NULL,
 version text NOT NULL, status text NOT NULL, level text, category text NOT NULL,
 fingerprint text, body text NOT NULL
);
CREATE INDEX privacy_slices_source ON privacy_slices(resource,source_version);
CREATE INDEX privacy_slices_matches ON privacy_slices(fingerprint) WHERE fingerprint IS NOT NULL;
CREATE TABLE matter_contacts (
 id text PRIMARY KEY, platform text NOT NULL, account text NOT NULL, thread text NOT NULL,
 actor text NOT NULL, anchor text NOT NULL, occurred text NOT NULL, permission text NOT NULL
);
CREATE TABLE role_matters (
 id text PRIMARY KEY, version text NOT NULL, status text NOT NULL, delivery text NOT NULL,
 platform text NOT NULL, account text NOT NULL, wake double precision NOT NULL, attempt text, body text NOT NULL
);
CREATE INDEX role_matters_wake ON role_matters(platform,account,status,wake);
CREATE TABLE matter_watches (
 matter text NOT NULL REFERENCES role_matters(id) ON DELETE CASCADE,
 contact text NOT NULL, PRIMARY KEY(matter,contact)
);
CREATE INDEX matter_watches_contact ON matter_watches(contact);
