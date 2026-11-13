"""Missing anchors may annotate partial reads; changed identities still stop."""
import pytest

from companion_wechat.db_source import SourceError
from tests_wechat.test_db_source import OWNER, message_table
from tests_wechat.test_db_source import lab as lab


def test_missing_lower_anchor_resumes_only_available_rows_and_marks_its_conversation(lab):
    lab.insert(1)
    with lab.session() as session:
        saved = session.snapshot()
    lab.insert(2)
    lab.shards["message_0.db"].execute(f'DELETE FROM "{message_table(OWNER)}" WHERE local_id=1')
    with lab.session(**saved, recover_missing_history=True) as session:
        window = session.freeze_window(saved["checkpoint"])
        result = session.read_window(saved["checkpoint"], window)
        assert [r["server_id"] for r in result["rows"]] == ["1002"]
        assert result["window_complete"]
        assert result["history_status"] == {
            "owner_main": {"status": "incomplete", "reasons": ["checkpoint_unavailable"]},
            "family": {"status": "complete", "reasons": []},
        }


def test_deleted_frozen_upper_advances_only_position_without_inventing_message(lab):
    lab.insert(1)
    with lab.session(recover_missing_history=True) as session:
        saved = session.snapshot()
        lab.insert(2)
        lab.insert(3)
        window = session.freeze_window(saved["checkpoint"])
        lab.shards["message_0.db"].execute(f'DELETE FROM "{message_table(OWNER)}" WHERE local_id IN (1,3)')
        first = session.read_window(saved["checkpoint"], window)
        assert [r["server_id"] for r in first["rows"]] == ["1002"]
        assert not first["window_complete"]
        last = session.read_window(first["next_checkpoint"], window)
        assert last["window_complete"] and last["rows"] == []
        assert last["history_status"]["owner_main"]["reasons"] == ["checkpoint_unavailable"]
        assert last["next_checkpoint"]["sources"]["message/message_0.db|owner_main"]["anchor"]["server_id"] == 1003


@pytest.mark.parametrize("field", ["server_id", "create_time"])
def test_recovery_does_not_accept_reused_or_changed_anchor(lab, field):
    lab.insert(1)
    with lab.session() as session:
        saved = session.snapshot()
    lab.insert(2)
    lab.shards["message_0.db"].execute(f'UPDATE "{message_table(OWNER)}" SET {field}={field}+1 WHERE local_id=1')
    with pytest.raises(SourceError, match="source_anchor_changed"):
        with lab.session(**saved, recover_missing_history=True):
            pass
