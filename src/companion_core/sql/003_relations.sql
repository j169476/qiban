CREATE TABLE actor_nodes(
 actor_key TEXT PRIMARY KEY, platform TEXT NOT NULL, identity_body TEXT NOT NULL,
 CHECK(left(identity_body,5)='enc1:'));
CREATE TABLE relationship_claims(
 id TEXT PRIMARY KEY,subject TEXT NOT NULL REFERENCES actor_nodes(actor_key),
 object TEXT NOT NULL REFERENCES actor_nodes(actor_key),declarer TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('claimed','confirmed','disputed','revoked')),
 version TEXT NOT NULL,body TEXT NOT NULL,CHECK(left(body,5)='enc1:'),CHECK(subject<>object));
CREATE INDEX relationship_subject ON relationship_claims(subject,status);
CREATE INDEX relationship_object ON relationship_claims(object,status);
