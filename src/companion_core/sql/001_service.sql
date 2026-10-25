CREATE TABLE schema_migrations(
 version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE record_kinds(kind TEXT PRIMARY KEY, schema_json JSONB NOT NULL, is_collection BOOLEAN NOT NULL);
CREATE TABLE content_records(
 kind TEXT NOT NULL REFERENCES record_kinds(kind), id TEXT NOT NULL,
 version TEXT NOT NULL CHECK(length(version)=64), revision BIGINT NOT NULL CHECK(revision>0),
 body BYTEA NOT NULL, locked BOOLEAN NOT NULL DEFAULT false,
 source_type TEXT NOT NULL DEFAULT '', source_ref TEXT NOT NULL DEFAULT '',
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(kind,id),
 CHECK(length(id) BETWEEN 1 AND 200), CHECK(octet_length(body)<=1100000));
CREATE INDEX content_source_ref ON content_records(source_ref) WHERE source_ref<>'';
CREATE INDEX content_kind_updated ON content_records(kind,updated_at DESC);
CREATE TABLE content_history(
 kind TEXT NOT NULL, id TEXT NOT NULL, version TEXT NOT NULL, body BYTEA NOT NULL,
 archived_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(kind,id,version));
CREATE TABLE content_audit(
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(),
 category TEXT NOT NULL, actor TEXT NOT NULL, resource TEXT NOT NULL,
 before_version TEXT, after_version TEXT, metadata JSONB NOT NULL DEFAULT '{}');

CREATE TABLE sessions(thread TEXT PRIMARY KEY,mode TEXT NOT NULL CHECK(mode IN ('admin','role')));
CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT NOT NULL CHECK(jsonb_typeof(value::jsonb) IS NOT NULL));
CREATE TABLE actions(
 id TEXT PRIMARY KEY,dedupe TEXT UNIQUE NOT NULL,owner TEXT NOT NULL,generation TEXT NOT NULL,
 account TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,
 day TEXT,units INTEGER NOT NULL CHECK(units>=0),target_uid TEXT,created TEXT NOT NULL,
 dispatched TEXT,result TEXT);
CREATE INDEX actions_account_day ON actions(account,day,status);
CREATE TABLE events(
 id TEXT NOT NULL,account TEXT NOT NULL,uid TEXT NOT NULL,kind TEXT NOT NULL,
 received TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(account,id));
CREATE TABLE relationship_pending(
 receipt TEXT PRIMARY KEY,uid TEXT UNIQUE NOT NULL,event_id TEXT NOT NULL,before_version TEXT,
 after_version TEXT NOT NULL,data TEXT NOT NULL,after_yaml TEXT NOT NULL,actor TEXT NOT NULL,
 created TEXT NOT NULL,status TEXT NOT NULL,error TEXT NOT NULL);
CREATE TABLE core_events(
 id TEXT PRIMARY KEY,platform TEXT NOT NULL,account TEXT NOT NULL,actor TEXT NOT NULL,
 conversation TEXT NOT NULL,occurred TEXT NOT NULL,body TEXT NOT NULL,version TEXT NOT NULL,
 deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)));
CREATE INDEX core_events_inbox ON core_events(platform,account,occurred,id) WHERE deleted=0;
CREATE INDEX core_events_thread ON core_events(conversation,occurred,id) WHERE deleted=0;
CREATE TABLE core_actions(
 id TEXT PRIMARY KEY,dedupe TEXT UNIQUE NOT NULL,generation TEXT NOT NULL,platform TEXT NOT NULL,
 account TEXT NOT NULL,capability TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,
 result TEXT,created TEXT NOT NULL);
CREATE INDEX core_actions_generation ON core_actions(generation,status);
CREATE TABLE core_identity_links(
 id TEXT PRIMARY KEY,left_actor TEXT NOT NULL,right_actor TEXT NOT NULL,status TEXT NOT NULL,version TEXT NOT NULL,
 CHECK(left_actor<>right_actor));
CREATE INDEX core_identity_left ON core_identity_links(left_actor,status);
CREATE INDEX core_identity_right ON core_identity_links(right_actor,status);
CREATE TABLE core_observations(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,stage TEXT NOT NULL,started TEXT NOT NULL,finished TEXT,
 status TEXT NOT NULL,error_code TEXT,duration_ms BIGINT,input_tokens BIGINT,cached_input_tokens BIGINT,
 output_tokens BIGINT,reasoning_output_tokens BIGINT,usage_events INTEGER NOT NULL DEFAULT 0);

CREATE TABLE governance_policies(
 resource TEXT PRIMARY KEY,version TEXT NOT NULL,classification TEXT NOT NULL,
 owners TEXT NOT NULL,conversation TEXT NOT NULL,audience TEXT NOT NULL,
 sources TEXT NOT NULL,valid INTEGER NOT NULL,expires_at DOUBLE PRECISION,source_versions TEXT NOT NULL,
 CHECK(classification IN ('public','conversation','personal','restricted','secret','unclassified')),
 CHECK(audience IN ('private','group','public')),CHECK(valid IN (0,1)));
CREATE TABLE governance_grants(
 id TEXT PRIMARY KEY,grantee TEXT NOT NULL,owner TEXT NOT NULL,resources TEXT NOT NULL,
 purposes TEXT NOT NULL,basis TEXT NOT NULL,evidence_id TEXT NOT NULL,expires_at DOUBLE PRECISION NOT NULL,
 epoch BIGINT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,created_at DOUBLE PRECISION NOT NULL,
 CHECK(basis IN ('owner_admin','account_control')),CHECK(revoked IN (0,1)));
CREATE INDEX governance_grants_recipient ON governance_grants(grantee,revoked,expires_at);
CREATE TABLE governance_audit(
 id TEXT PRIMARY KEY,at DOUBLE PRECISION NOT NULL,actor TEXT NOT NULL,operation TEXT NOT NULL,
 resource TEXT NOT NULL,before_version TEXT,after_version TEXT);
CREATE TABLE governance_sessions(
 id TEXT PRIMARY KEY,actor TEXT NOT NULL,purpose TEXT NOT NULL,expires_at DOUBLE PRECISION NOT NULL,
 epoch BIGINT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,CHECK(revoked IN (0,1)));
CREATE TABLE governance_challenges(
 id TEXT PRIMARY KEY,left_actor TEXT NOT NULL,right_actor TEXT NOT NULL,left_hash TEXT NOT NULL,right_hash TEXT NOT NULL,
 resources TEXT NOT NULL,purposes TEXT NOT NULL,expires_at DOUBLE PRECISION NOT NULL,epoch BIGINT NOT NULL,
 left_verified INTEGER NOT NULL DEFAULT 0,right_verified INTEGER NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0,consumed INTEGER NOT NULL DEFAULT 0);

-- These relational projections enforce ownership and grant scope independently
-- of the extension payload. They are populated transactionally by the repository.
CREATE TABLE record_owners(
 resource TEXT NOT NULL REFERENCES governance_policies(resource) ON DELETE CASCADE,
 actor TEXT NOT NULL,PRIMARY KEY(resource,actor));
CREATE INDEX record_owners_actor ON record_owners(actor,resource);
CREATE TABLE source_dependencies(
 derived TEXT NOT NULL REFERENCES governance_policies(resource) ON DELETE CASCADE,
 source TEXT NOT NULL REFERENCES governance_policies(resource),source_version TEXT NOT NULL,
 PRIMARY KEY(derived,source),CHECK(derived<>source));
CREATE INDEX source_dependencies_reverse ON source_dependencies(source,derived);
CREATE TABLE grant_resources(
 grant_id TEXT NOT NULL REFERENCES governance_grants(id) ON DELETE CASCADE,
 resource TEXT NOT NULL REFERENCES governance_policies(resource),PRIMARY KEY(grant_id,resource));
