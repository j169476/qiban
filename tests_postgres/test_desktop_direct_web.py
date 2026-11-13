"""Real task/database/HTTP-adapter integration with synthetic HTTP and accounts."""
import asyncio
import json
import secrets

import httpx
import pytest

from companion_codex import app_server
from companion_core.public_workspace import PublicWorkspace
from qiban.desktop_model import DesktopModel
from qiban.desktop_runtime import PREFERENCES, DesktopRuntime
from qiban.desktop_siwc import KEY
from qiban.desktop_siwc_accounts import DesktopSiwcAccounts
from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import seed_accounts
from tests_postgres.test_desktop_management import service as management_fixture
from tests_postgres.test_task_runner import NativeModel

URL = 'https://example.org/source'
service = management_fixture


class Stream(httpx.AsyncByteStream):
    def __init__(self, payload, silent, closed):
        self.payload, self.silent, self.on_close = payload, silent, closed
        self.closed = False

    async def __aiter__(self):
        if self.silent:
            await asyncio.Event().wait()
        yield self.payload

    async def aclose(self):
        self.closed = True
        self.on_close()


class Wire:
    def __init__(self, *, evidence=True, silent_first=False):
        self.evidence, self.silent_first = evidence, silent_first
        self.requests, self.streams = [], []
        self.entered = asyncio.Event()
        self.on_close = lambda: None

    def handle(self, request):
        body = json.loads(request.content)
        assert str(request.url) == 'https://api.openai.com/v1/responses'
        assert body['model'] == 'gpt-6.1-terra' and body['reasoning']['effort'] == 'low'
        assert body['store'] is False and body['stream'] is True
        assert [tool['type'] for tool in body['tools']] == ['web_search']
        self.requests.append(request.headers['authorization'])
        result = {'status': 'done', 'reason': '', 'image_path': '', 'sources': [{
            'url': URL, 'title': '合成资料🙂', 'excerpt': 'material with conditions',
            'note': '合成说明123🙂', 'tags': []}]}
        events = []
        if self.evidence:
            events.append({'type': 'response.output_item.done', 'item': {
                'id': 'ws_157', 'type': 'web_search_call', 'status': 'completed',
                'action': {'type': 'open_page', 'url': URL}}})
        events.extend([
            {'type': 'response.output_item.done', 'item': {
                'id': 'msg_157', 'type': 'message', 'status': 'completed', 'content': [
                    {'type': 'output_text', 'text': json.dumps(result, ensure_ascii=False)}]}},
            {'type': 'response.completed', 'response': {
                'id': 'resp_157', 'model': 'gpt-6.1-terra', 'output': [],
                'usage': {'input_tokens': 12, 'output_tokens': 7,
                          'input_tokens_details': {'cached_tokens': 4},
                          'output_tokens_details': {'reasoning_tokens': 2}}}},
        ])
        payload = ''.join('data: ' + json.dumps(e, ensure_ascii=False) + '\n\n' for e in events).encode()
        stream = Stream(payload, self.silent_first and not self.streams, lambda: self.on_close())
        self.streams.append(stream)
        self.entered.set()
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)


def prepare(service, tmp_path, monkeypatch, wire):
    book = seed_accounts(service.store)
    for account in book['accounts'].values():
        account['session']['expires_at'] = 10000
    service.store.set_state(KEY, book)

    def forbidden(*args, **kwargs):
        raise AssertionError('No network, credential refresh or real Codex worker allowed')

    service.codex = DesktopSiwcAccounts(tmp_path / 'accounts', service.store,
                                       clock=lambda: 1000, transport=forbidden)
    monkeypatch.setattr(app_server, 'generate', forbidden)
    # Any HTTP request that fails to use the injected transport is an error.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)
    model = DesktopModel(service.codex, executable='never-launch-real-codex',
                         web_transport=httpx.MockTransport(wire.handle))
    runtime = DesktopRuntime(service, 'direct-web-157', room_model=model)
    return runtime, book


def submit(runtime, kind='web'):
    arguments = ({'objective': 'learn conditions', 'criteria': ['apply'], 'round_limit': 2}
                 if kind == 'learn' else {'prompt': '合成要求，不访问公网'})
    return runtime.manual.submit(runtime.management.session_id, kind, arguments,
                                 secrets.token_hex(16))['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['web', 'learn'])
async def test_real_adapter_private_task_and_learning_routes(service, tmp_path, monkeypatch, kind):
    wire = Wire()
    runtime, book = prepare(service, tmp_path, monkeypatch, wire)
    before = service.store.state(PREFERENCES)
    nonweb = NativeModel(tmp_path)

    async def synthetic_app_server(instructions, context, schema, images=(), **options):
        assert options['native'] is None
        assert options['model'] == 'gpt-6.1-terra' and options['reasoning_effort'] == 'low'
        assert options['plan_access_token'] == 'access-1'
        options['guard']()
        return await nonweb.generate(instructions, context, schema, images)

    if kind == 'learn':
        monkeypatch.setattr(app_server, 'generate', synthetic_app_server)
    identifier = submit(runtime, kind)
    for _ in range(4):
        await runtime.tasks.run(runtime.manual.step)
    state = runtime.manual.engine._state(identifier)
    assert state['status'] == 'completed', state
    assert wire.requests == ['Bearer access-1']
    assert all(stream.closed for stream in wire.streams)
    assert len(nonweb.calls) == (3 if kind == 'learn' else 0)
    assert len(service.store.records('task_results')[0]) == (4 if kind == 'learn' else 1)
    assert not PublicWorkspace(service.store).records('task_results')[0]
    assert not PublicWorkspace(service.store).records('memories')[0]
    assert URL in runtime.manual.detail(service.session_id, identifier, 0)['text']
    assert service.store.state(PREFERENCES) == before and not runtime.cores
    assert service.store.state(KEY)['active'] == book['active']
    with service.store.db() as db:
        observations = [dict(row) for row in db.execute(
            "SELECT * FROM core_observations WHERE kind='model' AND input_tokens IS NOT NULL")]
    assert len(observations) == 1
    observed = observations[0]
    assert observed['status'] == 'success' and observed['usage_events'] == 1
    assert [observed[key] for key in ('input_tokens', 'output_tokens', 'cached_input_tokens',
                                    'reasoning_output_tokens')] == [12, 7, 4, 2]


@pytest.mark.asyncio
async def test_missing_open_proof_still_consumes_quota_and_never_saves_result(service, tmp_path, monkeypatch):
    wire = Wire(evidence=False)
    runtime, _ = prepare(service, tmp_path, monkeypatch, wire)
    settings = service.store.read('companion')
    service.store.write('companion', {**settings['data'], 'native_web_daily_limit': 1},
                        settings['version'], 'test')
    for code in ('native_web_evidence', 'native_quota'):
        identifier = submit(runtime)
        await runtime.tasks.run(runtime.manual.step)
        state = runtime.manual.engine._state(identifier)
        assert state['status'] == 'failed' and state['error'] == code, state
    assert wire.requests == ['Bearer access-1']
    assert not service.store.records('task_results')[0]


@pytest.mark.asyncio
async def test_cancel_silent_direct_request_closes_without_result_or_retry(service, tmp_path, monkeypatch):
    wire = Wire(silent_first=True)
    runtime, _ = prepare(service, tmp_path, monkeypatch, wire)
    identifier = submit(runtime)
    task = asyncio.create_task(runtime.tasks.run(runtime.manual.step))
    await asyncio.wait_for(wire.entered.wait(), 3)
    assert not task.done() and not wire.streams[0].closed
    state = runtime.manual.engine._state(identifier)
    runtime.manual.control(service.session_id, identifier, 'cancel', state['revision'])
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
    assert wire.streams[0].closed and wire.requests == ['Bearer access-1']
    assert runtime.manual.engine._state(identifier)['status'] == 'cancelled'
    assert not service.store.records('task_results')[0]
    assert not await runtime.tasks.run(runtime.manual.step)


@pytest.mark.asyncio
async def test_account_switch_drains_old_stream_before_new_account_resumes(service, tmp_path, monkeypatch):
    wire = Wire(silent_first=True)
    runtime, book = prepare(service, tmp_path, monkeypatch, wire)
    closed_accounts = []
    wire.on_close = lambda: closed_accounts.append(service.store.state(KEY)['active'])
    target = list(book['accounts'])[1]
    identifier = submit(runtime)
    task = asyncio.create_task(runtime.tasks.run(runtime.manual.step))
    await asyncio.wait_for(wire.entered.wait(), 3)
    assert not task.done() and not wire.streams[0].closed
    service.codex.begin('select', account_id=target, expected=1)
    await asyncio.to_thread(service.codex.thread.join, 8)
    assert not service.codex.thread.is_alive()
    await asyncio.wait_for(task, 5)
    state = runtime.manual.engine._state(identifier)
    assert state['status'] == 'completed', state
    assert wire.requests == ['Bearer access-1', 'Bearer access-2']
    assert closed_accounts == [book['active'], target]
    assert all(stream.closed for stream in wire.streams)
    assert service.store.state(KEY)['active'] == target
    assert len(service.store.records('task_results')[0]) == 1
    assert not await runtime.tasks.run(runtime.manual.step)
