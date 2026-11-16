"""SIWC execution through real synthetic stdio processes; never contacts a model."""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from test_desktop_siwc_accounts import choose, fixture
from test_plan_web import Transport, completed, done, message

from companion_codex import app_server
from companion_codex.isolation import IsolatedJob
from companion_core.errors import AssistantError
from qiban.desktop_model import DesktopModel

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}
SCRIPT = """import json,os,sys,time,tomllib
from pathlib import Path
mode=sys.argv[1]
home=Path(os.environ['CODEX_HOME'])
assert os.environ['ACCESS_TOKEN'].startswith('access-client-')
assert 'OPENAI_API_KEY' not in os.environ
def send(row):print(json.dumps(row),flush=True)
for line in sys.stdin:
 row=json.loads(line);method=row['method'];params=row.get('params',{})
 if method=='initialized':continue
 if method=='initialize':
  assert params['clientInfo']['name']=='Qiban'
  result={'codexHome':str(home)}
 elif method=='config/read':
  config=tomllib.loads((home/'config.toml').read_text())
  if mode=='wrong_provider':config['model_providers']['openai_chatgpt_plan']['base_url']='https://wrong.invalid'
  result={'config':config}
 elif method=='thread/start':result={'thread':{'id':'thread'}}
 elif method=='mcpServerStatus/list':result={'data':[]}
 elif method=='turn/start':
  assert params['model']=='gpt-6.1-terra' and params['effort']=='low'
  assert 'access-client-' not in json.dumps(params)
  (home/'started').write_text('yes')
  if mode=='wait' or (mode=='wait_first' and os.environ['ACCESS_TOKEN']=='access-client-a'):time.sleep(60)
  send({'id':row['id'],'result':{'turn':{'id':'turn'}}})
  answer='invalid-json' if mode=='invalid' else json.dumps({'value':42})
  send({'method':'item/completed','params':{'threadId':'thread','turnId':'turn','item':{'id':'answer','type':'agentMessage','text':answer}}})
  send({'method':'turn/completed','params':{'threadId':'thread','turn':{'id':'turn','status':'failed' if mode=='failed' else 'completed','error':{'codexErrorInfo':'usageLimitExceeded'}}}})
  continue
 else:raise AssertionError('Unexpected RPC: '+method)
 send({'id':row['id'],'result':result})
"""


def execution(tmp_path, monkeypatch, mode="success", web_transport=None):
    jobs, clients = [], []

    def forbidden(*args, **kwargs):
        raise AssertionError("Global Codex configuration/login must not be read")

    monkeypatch.setattr(app_server, "model_settings", forbidden)
    monkeypatch.setattr(app_server, "login_params", forbidden)

    def create(parent, **kwargs):
        job = IsolatedJob(parent, **kwargs)
        job.command = lambda: [sys.executable, "-u", "-c", SCRIPT, mode]
        jobs.append(job)
        return job

    monkeypatch.setattr(app_server, "IsolatedJob", create)
    original = app_server.RpcProcess.open

    async def opened(*args, **kwargs):
        client = await original(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(app_server.RpcProcess, "open", opened)
    broker, store, first, second = fixture(tmp_path / "accounts")
    model = DesktopModel(broker, executable=sys.executable, web_transport=web_transport)
    from qiban.desktop_model_catalog import KEY as CHOICE_KEY, parse_catalog
    original_state = store.state
    store.state = lambda key, default=None, db=None: {} if key == CHOICE_KEY else original_state(key, db=db)
    model.catalog.fetch = lambda access: parse_catalog({'models':[{'slug':'gpt-6.1-terra',
        'display_name':'Synthetic model','visibility':'list','default_reasoning_level':'low',
        'supported_reasoning_levels':[{'effort':'low'}]}]})

    async def run(native=None):
        return await model.generate(
            "Return JSON.",
            {"question": "synthetic"},
            SCHEMA,
            native=native,
            store=SimpleNamespace(runtime=tmp_path),
            policy={"worker_timeout_seconds": 3},
            guard=lambda: None,
        )

    return broker, store, first, second, model, run, jobs, clients


@pytest.mark.parametrize(
    "mode,error",
    [
        ("success", None),
        ("failed", "model_usage_limit"),
        ("invalid", "model_output_invalid"),
        ("wrong_provider", "model_config"),
    ],
)
async def test_real_protocol_result_or_failure_and_cleanup(tmp_path, monkeypatch, mode, error):
    broker, store, first, second, model, run, jobs, clients = execution(tmp_path, monkeypatch, mode)
    if error:
        with pytest.raises(AssistantError) as caught:
            await run()
        assert caught.value.code == error
        assert "access-client" not in str(caught.value)
    else:
        assert await run() == {"value": 42}
    assert len(jobs) == len(clients) == 1
    assert clients[0].process.poll() is not None
    assert not jobs[0].root.exists()
    assert store.value["active"] == first


async def wait_started(jobs):
    async with asyncio.timeout(3):
        while not jobs or not (jobs[-1].home / "started").exists():
            await asyncio.sleep(0.01)


async def wait_stream(transport):
    async with asyncio.timeout(3):
        while not transport.requests:
            await asyncio.sleep(0.01)


async def test_cancel_closes_worker_before_releasing_capacity(tmp_path, monkeypatch):
    *_, run, jobs, clients = execution(tmp_path, monkeypatch, "wait")
    task = asyncio.create_task(run())
    await wait_started(jobs)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clients[0].process.poll() is not None and not jobs[0].root.exists()


async def test_account_cutover_kills_old_process_before_new_token_is_committed(tmp_path, monkeypatch):
    from qiban.desktop_tasks import TaskBarrier

    broker, store, first, second, model, run, jobs, clients = execution(tmp_path, monkeypatch, "wait_first")
    barrier = TaskBarrier()

    def change(commit, *, ready):
        barrier.drain(barrier.block())
        assert clients[0].process.poll() is not None and not jobs[0].root.exists()
        assert store.value["active"] == first
        commit()
        barrier.release()

    broker.account_change = change
    task = asyncio.create_task(barrier.run(run))
    await wait_started(jobs)
    result = await asyncio.to_thread(choose, broker, second)
    assert result["active_account"] == second
    assert await asyncio.wait_for(task, 5) == {"value": 42}
    assert len(clients) == 2 and all(c.process.poll() is not None for c in clients)
    assert all(not job.root.exists() for job in jobs)


async def test_live_authorization_change_stops_worker_without_retry(tmp_path, monkeypatch):
    broker, store, first, second, model, run, jobs, clients = execution(tmp_path, monkeypatch, "wait")
    task = asyncio.create_task(run())
    await wait_started(jobs)
    store.value["accounts"][first]["session"]["access_token"] = "rotated-synthetic"
    with pytest.raises(AssistantError) as caught:
        await task
    assert caught.value.code == "model_auth"
    assert len(clients) == 1 and clients[0].process.poll() is not None
    assert not jobs[0].root.exists()


async def test_queued_work_gets_new_account_and_cancelled_queue_never_starts(tmp_path, monkeypatch):
    broker, store, first, second, model, run, jobs, clients = execution(tmp_path, monkeypatch)
    await model.capacity.acquire()
    queued = asyncio.create_task(run())
    cancelled = asyncio.create_task(run())
    await asyncio.sleep(0.02)
    assert not jobs
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    choose(broker, second)
    tokens = []
    original = broker.model_access

    def acquire():
        value = original()
        tokens.append(value.account_id)
        return value

    monkeypatch.setattr(broker, "model_access", acquire)
    model.capacity.release()
    assert await queued == {"value": 42}
    assert tokens == [second] and len(jobs) == 1


@pytest.mark.parametrize("change", ["selection", "logout", "rotation", "expiry", "uncertain"])
def test_access_snapshot_invalidates_without_exposing_or_refreshing_tokens(tmp_path, change):
    broker, store, first, second = fixture(tmp_path)
    access = broker.model_access()
    assert "access-client" not in repr(access)
    broker.check_model_access(access)
    account = store.value["accounts"][first]
    if change == "selection":
        choose(broker, second)
    elif change == "logout":
        account.pop("session")
    elif change == "rotation":
        account["session"]["access_token"] = "new-synthetic-token"
    elif change == "expiry":
        account["session"]["expires_at"] = 999
    else:
        account["renewal"] = {"status": "uncertain"}
    with pytest.raises(AssistantError) as caught:
        broker.check_model_access(access)
    assert caught.value.code == "model_auth"
    assert "access-client" not in json.dumps(broker.status())


async def test_signed_out_and_unsupported_tools_never_launch(tmp_path, monkeypatch):
    broker, store, first, second, model, run, jobs, clients = execution(tmp_path, monkeypatch)
    store.value["accounts"][first].pop("session")
    with pytest.raises(AssistantError):
        await run()
    with pytest.raises(AssistantError):
        await model.generate("", {}, SCHEMA, native="image", store=None, policy={}, guard=lambda: None)
    assert not jobs and not clients


async def test_web_step_uses_the_direct_route_not_the_app_server(tmp_path, monkeypatch):
    transport = Transport([done(message(json.dumps({"value": 42}))), completed()])
    broker, store, first, second, model, run, jobs, clients = execution(
        tmp_path, monkeypatch, web_transport=transport.transport)
    response = await run(native="web")
    assert response["result"] == {"value": 42}
    assert response["evidence"]["thread_id"] == "resp_1"
    assert response["evidence"]["opened"] == [] and response["evidence"]["web_calls"] == 0
    assert not jobs and not clients
    (request,) = transport.requests
    assert request["method"] == "POST" and request["url"] == "https://api.openai.com/v1/responses"
    assert request["authorization"] == "Bearer access-client-a"
    assert request["body"]["tools"] == [{"type": "web_search", "external_web_access": True}]
    assert "access-client-a" not in json.dumps(request["body"])
    assert store.value["active"] == first


async def test_web_failure_is_not_retried_or_fallen_back_to_the_app_server(tmp_path, monkeypatch):
    transport = Transport(status=401, body={"error": {"code": "unauthorized"}})
    broker, store, first, second, model, run, jobs, clients = execution(
        tmp_path, monkeypatch, web_transport=transport.transport)
    with pytest.raises(AssistantError) as caught:
        await run(native="web")
    assert caught.value.code == "model_auth"
    assert len(transport.requests) == 1 and not jobs and not clients
    assert "access-client" not in str(caught.value)


async def test_web_cancel_closes_the_stream_before_releasing_capacity(tmp_path, monkeypatch):
    transport = Transport([done(message(json.dumps({"value": 42})))], silent=True)
    *_, model, run, jobs, clients = execution(tmp_path, monkeypatch, web_transport=transport.transport)
    releases = []
    original = model.capacity.release
    model.capacity.release = lambda: (
        releases.append(transport.streams[0].closed if transport.streams else None), original())
    task = asyncio.create_task(run(native="web"))
    await wait_stream(transport)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.streams[0].closed and releases == [True]


async def test_web_account_cutover_closes_the_old_request_before_commit(tmp_path, monkeypatch):
    from qiban.desktop_tasks import TaskBarrier

    transport = Transport([done(message(json.dumps({"value": 42}))), completed()],
                          silent=True, silent_first=True)
    broker, store, first, second, model, run, jobs, clients = execution(
        tmp_path, monkeypatch, web_transport=transport.transport)
    barrier = TaskBarrier()

    def change(commit, *, ready):
        barrier.drain(barrier.block())
        assert transport.streams[0].closed
        assert store.value["active"] == first
        commit()
        barrier.release()

    broker.account_change = change
    task = asyncio.create_task(barrier.run(lambda: run(native="web")))
    await wait_stream(transport)
    assert not task.done() and not transport.streams[0].closed
    result = await asyncio.to_thread(choose, broker, second)
    assert result["active_account"] == second
    response = await asyncio.wait_for(task, 5)
    assert response["result"] == {"value": 42}
    assert response["evidence"]["thread_id"] == "resp_1"
    assert [request["authorization"] for request in transport.requests] == [
        "Bearer access-client-a", "Bearer access-client-b"]
    assert not jobs and not clients


async def test_queued_web_work_reads_the_new_account_and_cancelled_queue_sends_nothing(tmp_path, monkeypatch):
    transport = Transport([done(message(json.dumps({"value": 42}))), completed()])
    broker, store, first, second, model, run, jobs, clients = execution(
        tmp_path, monkeypatch, web_transport=transport.transport)
    await model.capacity.acquire()
    queued = asyncio.create_task(run(native="web"))
    cancelled = asyncio.create_task(run(native="web"))
    await asyncio.sleep(0.02)
    assert not transport.requests
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    choose(broker, second)
    model.capacity.release()
    response = await queued
    assert response["result"] == {"value": 42}
    assert response["evidence"]["thread_id"] == "resp_1"
    assert [request["authorization"] for request in transport.requests] == [
        "Bearer access-client-b"]
    assert not jobs
