"""Native message ordering survives sequences larger than a PostgreSQL int32."""
import pytest

from bili_assistant.connector import record_events
from bili_assistant.conversations import record_event
from companion_core.conversations import append
from companion_core.postgres import migrate
from tests_postgres.test_bili_web_lifecycle import pg_profile as pg_profile


def column_type(store):
    with store.db() as db:
        return db.execute("SELECT data_type FROM information_schema.columns WHERE table_schema=current_schema() "
            "AND table_name='conversation_index' AND column_name='sequence'").fetchone()[0]


@pytest.mark.parametrize('sequence', [2147483648, 2639687370121224, 9223372036854775807])
def test_large_native_sequence_archives_exactly_once(pg_store, sequence):
    event={'id':'dm-11-'+str(sequence),'uid':'11','kind':'dm_reply',
        'received':'2026-10-26T16:53:48+00:00','text':'synthetic regression'}
    record_events(pg_store,'123',[event])
    created=record_event(pg_store,'123',event)
    assert len(created)==1
    assert pg_store.read('conversation_turns',created[0])['data']['sequence']==sequence
    assert record_event(pg_store,'123',event)==[]
    with pg_store.db() as db:
        rows=db.execute('SELECT sequence FROM conversation_index WHERE kind=? AND id=?',
                        ('conversation_turns',created[0])).fetchall()
    assert len(rows)==1 and rows[0][0]==sequence


def test_upgrade_preserves_existing_projection_and_ciphertext_and_is_repeatable(pg_store):
    created=append(pg_store,'123','dm','11','small-native-7','synthetic existing','user','11',
        'received','2026-10-26T16:00:00+00:00',sequence=7)
    item=created[0]
    with pg_store.db() as db:
        before=db.execute('SELECT body,version FROM content_records WHERE kind=? AND id=?',
                          ('conversation_turns',item)).fetchone()
        # Reconstruct only this disposable schema's pre-010 migration state.
        db.execute('ALTER TABLE conversation_index ALTER COLUMN sequence TYPE INTEGER')
        db.execute('DELETE FROM schema_migrations WHERE version=10')
    assert column_type(pg_store)=='integer'
    migrate(pg_store.database,pg_store.profile)
    assert column_type(pg_store)=='bigint'
    migrate(pg_store.database,pg_store.profile)
    with pg_store.db() as db:
        after=db.execute('SELECT body,version FROM content_records WHERE kind=? AND id=?',
                         ('conversation_turns',item)).fetchone()
        assert tuple(before)==tuple(after)
        assert db.execute('SELECT sequence FROM conversation_index WHERE id=?',(item,)).fetchone()[0]==7
        assert db.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=10').fetchone()[0]==1
