import pytest

from bili_assistant.errors import AssistantError


@pytest.mark.asyncio
async def test_only_explicit_known_rejection_can_be_reprepared(role):
    async def rejected(*args):
        raise AssistantError("platform_rejected", "B站拒绝 (code=-403)")

    role.adapter.perform = rejected
    first = role.prepare("like", {"bvid": "BV1xx411c7mD"})
    result = await role.execute(first["id"])
    assert result["status"] == "failed"
    assert "code=-403" in result["result"]["message"]
    assert role.prepare("like", {"bvid": "BV1xx411c7mD"})["id"] == first["id"]
    second = role.prepare("like", {"bvid": "BV1xx411c7mD"}, retry_failed=True)
    assert second["id"] != first["id"]
    with role.store.db() as db:
        assert db.execute("SELECT status FROM actions WHERE id=?", (first["id"],)).fetchone()[0] == "failed"
    assert role.status()["quota"]["like"]["used"] == 0


@pytest.mark.asyncio
async def test_explicit_retry_cannot_resend_uncertain(role):
    role.adapter.fail = True
    first = role.prepare("like", {"bvid": "BV1xx411c7mD"})
    assert (await role.execute(first["id"]))["status"] == "uncertain"
    second = role.prepare("like", {"bvid": "BV1xx411c7mD"}, retry_failed=True)
    assert second["id"] == first["id"]
    assert not (await role.execute(second["id"]))["sent"]
    assert len(role.adapter.sent) == 1
