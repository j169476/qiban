import secrets
from types import SimpleNamespace

import pytest

from bili_assistant.connector import BilibiliConnector
from bili_assistant.engine import Engine
from bili_assistant.profiles import profile
from companion_core.contracts import ActorRef
from companion_core.governance import AdminService
from companion_core.profiles import default_profile
from companion_core.public_workspace import PublicWorkspace
from companion_core.runtime import Companion
from companion_core.task_contracts import SCHEMAS
from companion_core.tasks import ExplicitTasks
from companion_service.task_runner import TaskRunner
from tests_postgres.test_bili_governance import NOW, Platform, patch
from tests_postgres.test_runtime import Host

BVID = "BV1xx411c7mD"


@pytest.fixture
def pg_profile():
    value, common = profile(), default_profile()
    common.schemas.update(SCHEMAS)
    value.schemas.update(SCHEMAS)
    original = value.validator
    value.validator = lambda kind, data: common.validate(kind, data) if kind in SCHEMAS else original(kind, data)
    return value


class VideoPlatform(Platform):
    empty = False

    def __init__(self):
        super().__init__()
        self.windows = []

    async def video_context(self, bvid, page_index=0, detail=False, start_seconds=0, end_seconds=None):
        self.windows.append((start_seconds, end_seconds))
        return {"bvid": bvid, "cid": "123", "page_index": page_index, "title": "synthetic", "owner_uid": "888",
            "url": "https://www.bilibili.com/video/" + bvid, "images": [],
            "segments": [] if self.empty else [{"start": start_seconds, "end": end_seconds, "text": "actual material"}],
            "coverage": {"kind": "metadata_only" if self.empty else "subtitles", "bvid": bvid,
                "start_seconds": start_seconds, "end_seconds": end_seconds, "duration_seconds": 1800, "complete": False}}


class VideoModel:
    async def generate(self, instructions, context, schema, images=(), **options):
        assert "private-other" not in str(context) and not images
        return {"knowledge": "note from actual material", "tags": [], "video_topics": [], "video_gap": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("empty,revoke", [(False, False), (True, False), (False, True)])
async def test_private_video_windows_keep_sources_and_do_not_mutate_public_progress(pg_store, empty, revoke):
    from dataclasses import replace

    patch(pg_store, "manifest", account_uid="123")
    patch(pg_store, "companion", media_max_seconds=600)
    platform, host = VideoPlatform(), Host()
    platform.empty = empty
    connector = BilibiliConnector(Engine(pg_store, host, platform, lambda: NOW))
    core = Companion(pg_store, VideoModel(), host, [connector], clock=lambda: NOW)
    core.start()
    admin = AdminService(pg_store)
    actor = ActorRef("local", "owner", "web")

    def authenticate(actor, conversation, session):
        return admin.authority.issue(actor=actor, conversation=conversation, audience="private", recipients=(actor,))

    request = authenticate(actor, "private-video-task", "")
    manager = ExplicitTasks(core, admin.authority, authenticate=authenticate, session_active=lambda _: True,
        handler=TaskRunner(SimpleNamespace(core=core, store=pg_store, admin=admin)))
    identifier = manager.submit(request, actor, "video", {"bvid": BVID, "page_index": 0, "start_seconds": 0,
        "end_seconds": 1200, "mode": "text", "reason": "private learning requirement"}, secrets.token_hex(16))["id"]
    assert await manager.step()
    if empty:
        assert manager._state(identifier)["error"] == "video_material"
    else:
        assert manager._state(identifier)["status"] == "queued", manager._state(identifier)
        if revoke:
            record = pg_store.records("source_snapshots")[0][0]
            policy = admin.repository.policy("source_snapshots/" + record["data"]["id"])
            admin.repository.set_policy(replace(policy, version="corrected", valid=False), policy.version, "test")
        assert await manager.step()
        if revoke:
            assert manager._state(identifier)["status"] == "failed"
            assert len(platform.windows) == 1
        else:
            result = manager.detail(request, identifier)
            assert result["status"] == "completed", result
            assert result["result"]["payload"]["intervals"] == [[0, 1200]]
            assert platform.windows == [(0, 600), (600, 1200)]
            assert not result["result"]["payload"]["continuous_audio_visual_complete"]
    assert not platform.sent
    assert pg_store.state("context:" + BVID) is None
    assert not PublicWorkspace(pg_store).records("source_snapshots")[0]
    assert not PublicWorkspace(pg_store).records("video_progress")[0]
