"""Fixed SIWC provider configuration; access tokens never enter configuration files."""
import json

from companion_core.errors import AssistantError

NAME = 'openai_chatgpt_plan'
PROVIDER = {
    'name': 'ChatGPT plan', 'base_url': 'https://api.openai.com/v1',
    'env_key': 'ACCESS_TOKEN', 'wire_api': 'responses',
    'requires_openai_auth': False, 'supports_websockets': False,
    'request_max_retries': 0, 'stream_max_retries': 0,
}


def configure(job):
    path = job.home / 'config.toml'
    rows = [f'model_provider="{NAME}"', path.read_text(encoding='utf8'),
            f'[model_providers.{NAME}]']
    rows.extend(key + '=' + json.dumps(value) for key, value in PROVIDER.items())
    path.write_text('\n'.join(rows) + '\n', encoding='utf8')


def verify(config, model, effort):
    provider = config.get('model_providers', {}).get(NAME, {})
    if (config.get('model_provider') != NAME or config.get('model') != model
            or config.get('model_reasoning_effort') != effort
            or any(provider.get(key) != value for key, value in PROVIDER.items())):
        raise AssistantError('model_config', '订阅模型配置与本次选择不一致，未发送请求。')
