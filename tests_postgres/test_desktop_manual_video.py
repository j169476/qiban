"""Actual private video workflow with synthetic platform material and model."""
import pytest

from bili_assistant.connector import BilibiliConnector
from bili_assistant.engine import Engine
from qiban.desktop_runtime import PREFERENCES, DesktopRuntime
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service
from tests_postgres.test_desktop_manual_tasks import Bili, submit
from tests_postgres.test_private_video_tasks import VideoModel, VideoPlatform


@pytest.fixture
def pg_profile():
    from qiban.desktop_profile import desktop_profile
    return desktop_profile()


@pytest.mark.asyncio
async def test_desktop_video_uses_private_reader_while_platform_stays_paused(service, monkeypatch):
    from qiban import desktop_bili_connector
    from qiban.desktop_bili_policy import BiliRuntimeStore

    service.bili = Bili()
    platform = VideoPlatform()
    def adapter(broker):
        return BilibiliConnector(Engine(BiliRuntimeStore(service.store, '123'), None, platform), account='123')
    monkeypatch.setattr(desktop_bili_connector, 'adapter', adapter)
    runtime = DesktopRuntime(service, 'manual-video-test', room_model=VideoModel())
    errors = []
    original = runtime.manual.runner
    async def observed(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except Exception as error:
            errors.append(str(error))
            raise
    runtime.manual.runner = observed
    prefs = service.store.state(PREFERENCES)
    identifier = submit(runtime.manual)
    assert await runtime.tasks.run(runtime.manual.step)
    state = runtime.manual.engine._state(identifier)
    assert state['status'] == 'completed', (state, errors)
    result = runtime.manual.engine.detail(runtime.manual.request(service.session_id), identifier)
    assert result['result']['payload']['intervals'] == [[0, 60]]
    assert platform.windows == [(0, 60)] and not platform.sent
    assert service.store.state('core-run:bilibili', {}).get('status') != 'active'
    assert service.store.state('core-run:manual-video')['status'] == 'paused'
    assert service.store.state(PREFERENCES) == prefs
    assert service.store.state('context:BV1234567890') is None
    assert service.store.records('source_snapshots')[0]
