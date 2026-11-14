"""Which model slots the desktop worker assembles, and what a missing one still leaves usable.

``DesktopRuntime`` resolves QQ/WeChat through the generic ``model`` slot and local/Bilibili
through ``room_model``, so an assembly that fills only one slot leaves the platforms it did not
fill permanently ``model_ready`` False. These checks drive the worker's own assembly seam
(``storage_worker.desktop_models``) into the real resolvers and the real ``start`` admission
gate: no store, vault, PostgreSQL, platform traffic or model turn is opened, and the executable
is only ever named, never run.
"""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

# The probe scripts are executed from their directory, not installed as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'desktop_probe'))

import storage_worker  # noqa: E402

from companion_core.errors import AssistantError  # noqa: E402
from companion_core.runtime_keys import DESKTOP_PLATFORMS  # noqa: E402
from qiban import desktop_codex  # noqa: E402
from qiban.desktop_model import DesktopModel  # noqa: E402
from qiban.desktop_runtime import SESSION, DesktopRuntime  # noqa: E402
from qiban.desktop_tasks import TaskBarrier  # noqa: E402

EXECUTABLE = Path('synthetic') / 'codex.exe'


class Broker:
    """The account broker as readiness reads it; no token is ever exposed here."""

    def __init__(self, ready=True):
        self.ready, self.reads = ready, 0

    def ready_for_model(self):
        self.reads += 1
        return self.ready


class Core:
    """A platform companion that records whether admission let it start."""

    def __init__(self):
        self.platforms = SimpleNamespace(all=lambda: [SimpleNamespace(account='synthetic-account')])
        self.started = []

    def start(self, **options):
        self.started.append(options)
        return {'generation': 'synthetic'}


class Store:
    """Only the persisted session that ``start`` reads before it consults the model."""

    def __init__(self):
        self.lock = threading.RLock()
        self.values = {}

    def state(self, key, default=None, db=None):
        return self.values.get(key, default)


def assembled(slots, broker, platforms=DESKTOP_PLATFORMS):
    """A runtime carrying the worker's slots and only the fields ``__init__`` derives from them."""
    runtime = object.__new__(DesktopRuntime)
    runtime.management = SimpleNamespace(codex=broker, qq=None, wechat_resources=None)
    runtime.model, runtime.room_model = slots['model'], slots['room_model']
    runtime.application, runtime.instance, runtime.closed = 'wiring-app', 'wiring-instance', False
    runtime.role = SimpleNamespace(as_dict=lambda: {'key': 'synthetic-role'})
    runtime.reconfiguring, runtime.scheduler = set(), None
    runtime.tasks = TaskBarrier()
    runtime.cores = {platform: Core() for platform in platforms}
    runtime.store = Store()
    runtime.store.values[SESSION] = {'application': runtime.application,
                                     'instance': runtime.instance, 'held': []}
    return runtime


def test_siwc_validation_hands_every_platform_one_owned_model(monkeypatch):
    monkeypatch.setattr(desktop_codex, 'executable', lambda: EXECUTABLE)
    broker = Broker()
    slots = storage_worker.desktop_models(broker, 'siwc-validation')
    model = slots['model']
    assert set(slots) == {'model', 'room_model'} and isinstance(model, DesktopModel)
    assert model.broker is broker and model.executable == EXECUTABLE
    assert slots['room_model'] is model
    runtime = assembled(slots, broker)
    for platform in DESKTOP_PLATFORMS:
        assert runtime.platform_model(platform) is model
        assert runtime.model_ready(platform) is True
        # The admission gate really opens for every platform, not just for the room pair.
        assert runtime.start(platform)['started'] is True
        assert runtime.cores[platform].started == [{'read_only': False}]
    assert len({id(runtime.platform_model(platform)) for platform in DESKTOP_PLATFORMS}) == 1
    assert runtime.model_ready() is True


def test_signed_out_subscription_keeps_the_executor_bound_but_never_starts(monkeypatch):
    monkeypatch.setattr(desktop_codex, 'executable', lambda: EXECUTABLE)
    broker = Broker(ready=False)
    slots = storage_worker.desktop_models(broker, 'siwc-validation')
    runtime = assembled(slots, broker)
    for platform in DESKTOP_PLATFORMS:
        # Still bound, so a later account selection resumes through the ledgers.
        assert runtime.platform_model(platform) is slots['model']
        assert runtime.model_ready(platform) is False
        with pytest.raises(AssistantError) as error:
            runtime.start(platform)
        assert error.value.code == 'model_unavailable'
        assert runtime.cores[platform].started == []


def test_plain_codex_route_injects_no_model_and_never_locates_an_executable(monkeypatch):
    def forbidden():
        raise AssertionError('the plain Codex route must not locate an executable')

    monkeypatch.setattr(desktop_codex, 'executable', forbidden)
    broker = Broker()
    slots = storage_worker.desktop_models(broker, 'codex')
    assert slots == {'model': None, 'room_model': None}
    runtime = assembled(slots, broker)
    for platform in DESKTOP_PLATFORMS:
        assert runtime.platform_model(platform) is None
        assert runtime.model_ready(platform) is False
        with pytest.raises(AssistantError) as error:
            runtime.start(platform)
        assert error.value.code == 'model_unavailable'
        assert runtime.cores[platform].started == []
    # A model-less assembly cannot be papered over by a subscription that reports ready.
    assert broker.reads == 0 and runtime.model_ready() is False


def test_missing_executable_assembles_empty_slots_instead_of_failing_the_worker(monkeypatch):
    def missing():
        raise AssistantError('model_unavailable', '未找到 Codex，请安装后重新检查')

    monkeypatch.setattr(desktop_codex, 'executable', missing)
    broker = Broker()
    # Returning here is what keeps the store, local management and recovery serving; only
    # the platform admission gate refuses to launch work it cannot execute.
    slots = storage_worker.desktop_models(broker, 'siwc-validation')
    assert slots == {'model': None, 'room_model': None}
    runtime = assembled(slots, broker)
    for platform in DESKTOP_PLATFORMS:
        assert runtime.platform_model(platform) is None
        assert runtime.model_ready(platform) is False
        with pytest.raises(AssistantError) as error:
            runtime.start(platform)
        assert error.value.code == 'model_unavailable'
        assert runtime.cores[platform].started == []


def test_other_assembly_failures_still_abort_the_worker(monkeypatch):
    def broken():
        raise AssistantError('storage_config', 'synthetic assembly failure')

    monkeypatch.setattr(desktop_codex, 'executable', broken)
    with pytest.raises(AssistantError) as error:
        storage_worker.desktop_models(Broker(), 'siwc-validation')
    assert error.value.code == 'storage_config'
