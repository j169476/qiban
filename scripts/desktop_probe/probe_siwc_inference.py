"""One explicit synthetic SIWC turn through OS-isolated Codex; no credential files."""
import argparse
import asyncio
import json
import time
from pathlib import Path

from jsonschema import validate
from PIL import Image, ImageDraw
from storage_worker import credentials

from companion_codex.app_server import RpcProcess, TurnEvidence, verify_configuration
from companion_codex.isolation import IsolatedJob
from companion_core.errors import AssistantError
from companion_core.postgres import PostgresStore
from qiban.desktop_codex import executable
from qiban.desktop_siwc import KEY
from qiban.desktop_siwc_accounts import selected


def access(config_path):
    config = json.loads(config_path.read_text(encoding='utf8'))
    key, password = credentials(config['installation'])
    store = PostgresStore(config['data'], {**config['database'], 'password': password}, key)
    try:
        with store.db() as db:
            db.execute('SET TRANSACTION READ ONLY')
            value = selected(store.state(KEY, db=db) or {})
    finally:
        store.database.close()
    session = value.get('session', {})
    if (value.get('renewal', {}).get('status') not in (None, 'complete', 'temporary_failure')
            or 'chatgpt.tokens.use.direct' not in session.get('scopes', [])
            or session.get('expires_at', 0) <= time.time() + 180):
        raise AssistantError('siwc_reauthorization_required', 'Authorization not ready')
    return session['access_token']


async def run(args, report):
    token = access(args.config)
    job = IsolatedJob(args.evidence.parent / 'isolated-jobs', executable=executable(),
                      settings={'model':'gpt-6.1-terra', 'model_reasoning_effort':'low'})
    client = None
    evidence = TurnEvidence()
    try:
        config = job.home / 'config.toml'
        config.write_text('model_provider="openai_chatgpt_plan"\n' + config.read_text(encoding='utf8') +
            '\n[model_providers.openai_chatgpt_plan]\nname="ChatGPT plan"\n'
            'base_url="https://api.openai.com/v1"\nenv_key="ACCESS_TOKEN"\nwire_api="responses"\n'
            'requires_openai_auth=false\nsupports_websockets=false\nrequest_max_retries=0\nstream_max_retries=0\n',
            encoding='utf8')
        env = job.environment()
        env['ACCESS_TOKEN'] = token
        report['phase'] = 'isolated_process_start'
        client = await RpcProcess.open(job.command(), cwd=job.work, env=env, guard=lambda:None,
                                       deadline=time.monotonic()+150)
        initialized = await client.call('initialize', {'clientInfo':{'name':'Qiban','title':'Qiban','version':'0.9.4'},
                                                       'capabilities':{'experimentalApi':True}})
        if Path(initialized.get('codexHome','')).resolve() != job.home:
            raise AssistantError('model_isolation', 'Wrong isolated home')
        client.send({'method':'initialized'})
        report['phase'] = 'configuration_check'
        effective = (await client.call('config/read', {'includeLayers':False}))['config']
        verify_configuration(effective, None)
        if effective.get('model_provider') != 'openai_chatgpt_plan' or effective.get('model') != 'gpt-6.1-terra':
            raise AssistantError('model_config', 'Unexpected provider or model')
        if effective.get('model_reasoning_effort') != 'low':
            raise AssistantError('model_config', 'Unexpected effort')
        report['isolated_configuration_verified'] = True
        report['phase'] = 'thread_start'
        thread = await client.call('thread/start', {'cwd':str(job.work), 'ephemeral':True,
            'model':'gpt-6.1-terra', 'approvalPolicy':'never', 'sandbox':'read-only',
            'developerInstructions':'Return only the requested JSON. Do not use tools.',
            'environments':[], 'selectedCapabilityRoots':[]}, evidence.update)
        evidence.thread_id = thread['thread']['id']
        servers = await client.call('mcpServerStatus/list', {})
        if servers.get('data') != [] or servers.get('nextCursor'):
            raise AssistantError('model_isolation', 'Unexpected tool servers')
        inputs = [{'type':'text','text':'Calculate 19+23. Return sum and label, with label set to 栖伴.'}]
        expected = {'sum':42, 'label':'栖伴'}
        if args.case == 'image':
            picture = Image.new('RGB', (600,300), 'white')
            draw = ImageDraw.Draw(picture)
            for x in (30,210,390):
                draw.ellipse((x,30,x+100,130), fill='blue')
            for x in (100,340):
                draw.rectangle((x,180,x+100,280), fill='red')
            path = job.work/'synthetic-shapes.png'
            picture.save(path)
            inputs = [{'type':'text','text':'Count the blue circles and red squares in the attached image. Return blue_circles and red_squares.'},
                      {'type':'localImage','path':str(path)}]
            expected = {'blue_circles':3, 'red_squares':2}
        schema = {'type':'object','properties':{key:{'type':'integer' if isinstance(value,int) else 'string'}
                  for key,value in expected.items()}, 'required':list(expected), 'additionalProperties':False}
        def observe(row):
            if row.get('method') in ('error','turn/completed'):
                # Extract only fixed diagnostics, never raw error strings or transport bodies.
                raw = json.dumps(row)
                codes = ('subscription_sharing_usage_limit_exceeded','subscription_sharing_usage_unavailable',
                         'subscription_sharing_unsupported_capability','insufficient_quota','model_not_found',
                         'invalid_api_key','invalid_request_error')
                report['provider_errors'] = [code for code in codes if code in raw]
            evidence.update(row)
        report['phase'] = 'turn_start'
        report['turn_requests'] = 1
        turn = await client.call('turn/start', {'threadId':evidence.thread_id, 'input':inputs,
            'model':'gpt-6.1-terra', 'effort':'low', 'outputSchema':schema}, observe)
        evidence.turn_id = turn['turn']['id']
        while not evidence.completed:
            row = await client.receive()
            client.reject_request(row)
            observe(row)
        value = json.loads(evidence.answer)
        validate(value, schema)
        report.update(completed=True, structured_result_valid=True, exact_match=value==expected,
                      phase='completed', usage=evidence.usage)
    finally:
        if client:
            await asyncio.to_thread(client.close)
        job.close()
        report['owned_processes_closed'] = True
        report['job_directory_removed'] = not job.root.exists()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--case',choices=('text','image'),required=True)
    args = parser.parse_args()
    if args.evidence.exists():
        raise FileExistsError('Evidence already exists')
    report = {'case':args.case, 'model':'gpt-6.1-terra', 'effort':'low', 'completed':False,
              'turn_requests':0,'api_key_used':False,'platform_messages_sent':False}
    started = time.monotonic()
    try:
        asyncio.run(run(args, report))
    except AssistantError as error:
        report['error'] = error.code
    except Exception:
        report['error'] = 'probe_failed'
    report['elapsed_seconds'] = round(time.monotonic()-started,3)
    args.evidence.write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
