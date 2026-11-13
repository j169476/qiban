"""Read the selected SIWC account's catalog without inference or credential logging."""
import argparse
import json
import re
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from storage_worker import credentials

from companion_core.postgres import PostgresStore
from qiban.desktop_siwc import KEY, NoRedirect
from qiban.desktop_siwc_accounts import selected


def inspect(config_path):
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
    report = {'model': 'gpt-6.1-terra', 'effort': 'low', 'inference_requests': 0,
              'credential_source': 'application_encrypted_store', 'api_key_used': False}
    if value.get('renewal', {}).get('status') not in (None, 'complete', 'temporary_failure'):
        return {**report, 'status': 'account_recovery_required'}
    if 'chatgpt.tokens.use.direct' not in session.get('scopes', []):
        return {**report, 'status': 'subscription_permission_missing'}
    if session.get('expires_at', 0) <= time.time() + 30:
        return {**report, 'status': 'reauthorization_required'}
    query = Request('https://api.openai.com/v1/models', headers={
        'Authorization': 'Bearer ' + session['access_token'], 'Accept': 'application/json'})
    try:
        with build_opener(NoRedirect()).open(query, timeout=25) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                return {**report, 'status': 'catalog_response_too_large'}
            result = json.loads(raw)
    except HTTPError as error:
        try:
            body = json.loads(error.read(16384))
            code = body.get('error', {}).get('code')
        except (ValueError, AttributeError):
            code = None
        safe = code if isinstance(code, str) and re.fullmatch('[a-z_]{1,100}', code) else 'unclassified'
        return {**report, 'status': 'catalog_rejected', 'http_status': error.code, 'error_code': safe}
    if not isinstance(result.get('models'), list):
        return {**report, 'status': 'unexpected_catalog_schema'}
    models = [{'slug': item.get('slug'), 'visibility': item.get('visibility')}
              for item in result['models'] if isinstance(item, dict)]
    available = any(item['slug'] == report['model'] for item in models)
    return {**report, 'status': 'requested_model_listed' if available else 'requested_model_not_listed',
            'models': models, 'inference_access_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if args.evidence.exists():
        raise FileExistsError('Evidence already exists')
    try:
        result = inspect(args.config.resolve(strict=True))
    except Exception:
        result = {'status': 'catalog_check_failed', 'inference_requests': 0}
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
