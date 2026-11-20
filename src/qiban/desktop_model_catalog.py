"""Account-scoped upstream model catalog and application-owned selection."""
import json
import re
import secrets
import threading
import time
from urllib.request import Request, build_opener

from companion_core.errors import AssistantError

from .desktop_siwc import NoRedirect

KEY = 'desktop-model-choice-v1'
IDENTIFIER = re.compile(r'[A-Za-z0-9_.:/-]{1,128}\Z')


def fetch_catalog(access):
    try:
        request = Request('https://api.openai.com/v1/models', headers={
            'Authorization': 'Bearer ' + access.access_token, 'Accept': 'application/json'})
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise ValueError('catalog too large')
            return parse_catalog(json.loads(raw))
    except AssistantError:
        raise
    except Exception:
        raise AssistantError('model_catalog', '模型列表读取失败，请稍后刷新。') from None


def parse_catalog(value):
    rows = value.get('models', []) if isinstance(value, dict) else []
    found = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('visibility') != 'list':
            continue
        model = row.get('slug')
        if not isinstance(model, str) or not IDENTIFIER.fullmatch(model):
            continue
        levels = row.get('supported_reasoning_levels')
        if not isinstance(levels, list):
            continue
        efforts = [e['effort'] for e in levels
                   if isinstance(e, dict) and isinstance(e.get('effort'), str)
                   and IDENTIFIER.fullmatch(e['effort'])]
        default = row.get('default_reasoning_level')
        if ((levels and default not in efforts) or (not levels and default is not None)
                or 'text' not in row.get('input_modalities', ['text'])):
            continue
        name = row.get('display_name')
        priority = row.get('priority', 10000)
        tiers = row.get('service_tiers')
        if not isinstance(tiers, list):
            tiers = row.get('additional_speed_tiers') or []
        ultrafast = isinstance(tiers, list) and any(
            (tier.get('id') if isinstance(tier, dict) else tier) == 'ultrafast' for tier in tiers)
        found[model] = {'id': model, 'name': name[:128] if isinstance(name, str) else model,
                        'ultrafast': ultrafast,
                        'efforts': list(dict.fromkeys(efforts)), 'default_effort': default,
                        'priority': priority if type(priority) is int else 10000}
    if not found:
        raise AssistantError('model_catalog_empty', '上游暂未返回可用模型，请稍后刷新。')
    return sorted(found.values(), key=lambda r: (r['priority'], r['id']))


class ModelCatalog:
    def __init__(self, broker, fetch=fetch_catalog):
        self.broker, self.fetch = broker, fetch
        self.lock = threading.RLock()
        self.cache = {}

    def rows(self, access, *, refresh=False):
        with self.lock:
            cached = self.cache.get(access.account_id)
            if refresh or not cached or time.monotonic() - cached[0] > 1800:
                rows = self.fetch(access)
                self.broker.check_model_access(access)
                self.cache[access.account_id] = (time.monotonic(), rows)
            return self.cache[access.account_id][1]

    def status(self, store, *, refresh=False, access=None):
        access = access or self.broker.model_access()
        rows = self.rows(access, refresh=refresh)
        self.broker.check_model_access(access)
        saved = store.state(KEY, {}).get(access.account_id, {})
        model = saved.get('model', rows[0]['id'])
        row = next((r for r in rows if r['id'] == model), None)
        effort = saved.get('effort', row['default_effort'] if row else '')
        ultrafast = saved.get('ultrafast') is True
        return {'account_id': access.account_id, 'revision': saved.get('revision', ''),
                'models': rows, 'model': model, 'effort': effort, 'ultrafast': ultrafast,
                'available': bool(row and effort in (row['efforts'] or [None])
                                  and (not ultrafast or row.get('ultrafast')))}

    def configure(self, store, *, account_id, expected, model, effort, ultrafast=False):
        with self.lock:
            return self._configure(store, account_id, expected, model, effort, ultrafast)

    def _configure(self, store, account_id, expected, model, effort, ultrafast):
        access = self.broker.model_access()
        status = self.status(store, access=access)
        if access.account_id != account_id or status['revision'] != expected:
            raise AssistantError('edit_conflict', '模型账号或选择已变化，请刷新列表。')
        row = next((r for r in status['models'] if r['id'] == model), None)
        if not row or effort not in (row['efforts'] or [None]):
            raise AssistantError('model_selection', '请选择列表中的模型和推理档位。')
        if type(ultrafast) is not bool or ultrafast and not row.get('ultrafast'):
            raise AssistantError('model_selection', '此模型当前不支持所选速度。')
        with store.lock, store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.broker.check_model_access(access)
            values = store.state(KEY, {}, db)
            if values.get(account_id, {}).get('revision', '') != expected:
                raise AssistantError('edit_conflict', '模型选择已变化，请刷新列表。')
            if expected and (status['model'], status['effort'], status['ultrafast']) == (model, effort, ultrafast):
                return status
            values[account_id] = dict(model=model, effort=effort, ultrafast=ultrafast, revision=secrets.token_hex(16))
            store.set_state(KEY, values, db)
        return self.status(store, access=access)

    def resolve(self, store, access):
        status = self.status(store, access=access)
        if not status['available']:
            raise AssistantError('model_selection', '所选模型已不可用，请在模型与用量中重新选择。')
        return status['model'], status['effort'], 'ultrafast' if status['ultrafast'] else None
