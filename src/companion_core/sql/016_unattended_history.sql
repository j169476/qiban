-- Disposable oldest-first projection. All payloads stay in their original tables.
-- Existing projection rows for memories remain valid. Rescan to include event and chat pins.
DELETE FROM state WHERE key='source-retention-index-v1';
ALTER TABLE actions ADD COLUMN history_pruned boolean NOT NULL DEFAULT false;
ALTER TABLE core_actions ADD COLUMN history_pruned boolean NOT NULL DEFAULT false;
ALTER TABLE core_events ADD COLUMN history_pruned boolean NOT NULL DEFAULT false;
ALTER TABLE media_originals ADD COLUMN expired_at double precision;
CREATE INDEX pending_actions_for_history ON actions(account) WHERE status IN ('prepared','dispatching','uncertain');
CREATE INDEX pending_core_actions_for_history ON core_actions(platform,account) WHERE status IN ('prepared','dispatching','uncertain');
CREATE INDEX desktop_notice_maintenance ON state(key) WHERE key LIKE 'desktop-notice:%' OR key LIKE 'desktop-notice-incident:%';
UPDATE media_originals SET expired_at=extract(epoch FROM now()) WHERE status='expired';

CREATE TABLE ordinary_history (
 family text NOT NULL, k1 text NOT NULL, k2 text NOT NULL, k3 text NOT NULL,
 stamp timestamptz NOT NULL, bytes bigint NOT NULL, retry_at timestamptz,
 PRIMARY KEY(family,k1,k2,k3)
);
CREATE INDEX ordinary_history_oldest ON ordinary_history(stamp,family,k1,k2,k3);
CREATE TABLE ordinary_history_total(singleton boolean PRIMARY KEY CHECK(singleton), bytes bigint NOT NULL);
INSERT INTO ordinary_history_total VALUES(true,0);

CREATE FUNCTION ordinary_history_size() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
 UPDATE ordinary_history_total SET bytes=bytes+COALESCE(NEW.bytes,0)-COALESCE(OLD.bytes,0);
 RETURN NULL;
END;
$$;
CREATE TRIGGER ordinary_history_size AFTER INSERT OR DELETE OR UPDATE OF bytes ON ordinary_history
 FOR EACH ROW EXECUTE FUNCTION ordinary_history_size();

-- Use the same expressions for existing data and subsequent writes. Identifiers and expressions
-- below are migration constants. No payload is decoded to build the retention projection.
DO $migration$
DECLARE spec record; projection text; predicate text; erase text; upsert text;
BEGIN
 FOR spec IN SELECT * FROM (VALUES
  ('content_records','r.kind','r.id',$e$''$e$,'r.updated_at','octet_length(r.body)',
    $e$r.kind IN ('conversation_turns','conversation_summaries') AND NOT r.locked$e$),
  ('content_history','r.kind','r.id','r.version','r.archived_at','octet_length(r.body)',
    $e$r.kind IN ('conversation_turns','conversation_summaries')$e$),
  ('content_audit','r.id::text',$e$''$e$,$e$''$e$,'r.at','pg_column_size(r)','true'),
  ('governance_audit','r.id',$e$''$e$,$e$''$e$,'to_timestamp(r.at)','pg_column_size(r)',
    $e$NOT (r.actor='personal-fact-projection' AND r.before_version IS NULL)$e$),
  ('core_events','r.id',$e$''$e$,$e$''$e$,'r.occurred::timestamptz','octet_length(r.body)','NOT r.history_pruned'),
  ('events','r.account','r.id',$e$''$e$,'r.received::timestamptz','octet_length(r.body)','true'),
  ('actions','r.id',$e$''$e$,$e$''$e$,'r.created::timestamptz',
    'octet_length(r.payload)+coalesce(octet_length(r.result),0)',
    'NOT r.history_pruned'),
  ('core_actions','r.id',$e$''$e$,$e$''$e$,'r.created::timestamptz',
    'octet_length(r.body)+coalesce(octet_length(r.result),0)',
    'NOT r.history_pruned')
 ) AS s(tab,k1,k2,k3,stamp,bytes,eligible)
 LOOP
  projection:=format('%L,%s,%s,%s,%s,%s',spec.tab,spec.k1,spec.k2,spec.k3,spec.stamp,spec.bytes);
  predicate:=spec.eligible;
  upsert:=' ON CONFLICT(family,k1,k2,k3) DO UPDATE SET bytes=excluded.bytes,stamp=excluded.stamp,retry_at=NULL';
  EXECUTE 'INSERT INTO ordinary_history(family,k1,k2,k3,stamp,bytes) SELECT '||projection||
          ' FROM '||quote_ident(spec.tab)||' r WHERE '||predicate;
  erase:=format('DELETE FROM ordinary_history WHERE family=%L AND k1=%s AND k2=%s AND k3=%s',
                spec.tab,spec.k1,spec.k2,spec.k3);
  EXECUTE format('CREATE FUNCTION %I() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS %L',
   'track_history_'||spec.tab,
   'BEGIN IF TG_OP=''DELETE'' THEN '||replace(erase,'r.','OLD.')||'; ELSIF '||
   replace(predicate,'r.','NEW.')||' THEN INSERT INTO ordinary_history(family,k1,k2,k3,stamp,bytes) SELECT '||
   replace(replace(projection,'r.','NEW.'),'pg_column_size(r)','pg_column_size(NEW)')||upsert||
   '; ELSE '||replace(erase,'r.','NEW.')||'; END IF; RETURN NULL; END;');
  EXECUTE format('CREATE TRIGGER track_history AFTER INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION %I()',
                 spec.tab,'track_history_'||spec.tab);
 END LOOP;
END;
$migration$;

-- Imports and edits can write a turn today whose actual conversation happened long ago.
CREATE FUNCTION ordinary_conversation_stamp() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
 UPDATE ordinary_history SET stamp=to_timestamp(NEW.stamp) WHERE family='content_records'
  AND k1=NEW.kind AND k2=NEW.id AND k3='';
 RETURN NULL;
END;
$$;
CREATE TRIGGER ordinary_conversation_stamp AFTER INSERT OR UPDATE ON conversation_index
 FOR EACH ROW EXECUTE FUNCTION ordinary_conversation_stamp();
UPDATE ordinary_history h SET stamp=to_timestamp(i.stamp) FROM conversation_index i
 WHERE h.family='content_records' AND h.k1=i.kind AND h.k2=i.id;

