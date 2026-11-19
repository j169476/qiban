import copy
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from companion_core.errors import AssistantError
from qiban.desktop_activity import Activity
from qiban.desktop_model_catalog import ModelCatalog, parse_catalog


def upstream(name, default='medium', priority=1, visibility='list'):
    return dict(slug=name, display_name=name, visibility=visibility, priority=priority,
                default_reasoning_level=default, input_modalities=['text'],
                supported_reasoning_levels=[{'effort': 'low'}, {'effort': 'medium'}, {'effort': 'high'}])


class Store:
    def __init__(self):
        self.values = {}
        self.lock = threading.RLock()

    @contextmanager
    def db(self):
        before = copy.deepcopy(self.values)
        try:
            yield SimpleNamespace(execute=lambda query: None)
        except Exception:
            self.values = before
            raise

    def state(self, key, default=None, db=None):
        return copy.deepcopy(self.values.get(key, default))

    def set_state(self, key, value, db=None):
        self.values[key] = copy.deepcopy(value)


def fixture():
    access = SimpleNamespace(account_id='a', access_token='synthetic')
    def check(value):
        if value.account_id != broker.active:
            raise AssistantError('model_auth','changed')
    broker = SimpleNamespace(active='a', model_access=lambda: SimpleNamespace(account_id=broker.active, access_token='synthetic'), check_model_access=check)
    calls = []
    def fetch(value):
        calls.append(value.account_id)
        return parse_catalog({'models': [upstream('upstream-b','high',2),upstream('upstream-a'),upstream('hidden',visibility='hide')]})
    return ModelCatalog(broker, fetch), broker, Store(), calls, access


def test_upstream_defaults_refresh_and_account_isolation():
    catalog, broker, store, calls, access = fixture()
    value = catalog.status(store)
    assert value['model'] == 'upstream-a' and value['effort'] == 'medium'
    assert len(value['models']) == 2
    saved = catalog.configure(store, account_id='a', expected='', model='upstream-b', effort='high')
    assert catalog.resolve(store,access) == ('upstream-b','high')
    assert calls == ['a']
    catalog.status(store, refresh=True)
    assert calls == ['a', 'a']
    with pytest.raises(AssistantError,match='变化'):
        catalog.configure(store,account_id='a',expected='',model='upstream-a',effort='low')
    with pytest.raises(AssistantError,match='档位'):
        catalog.configure(store,account_id='a',expected=saved['revision'],model='upstream-b',effort='unsupported')
    broker.active='b'
    assert catalog.status(store)['model'] == 'upstream-a'
    with pytest.raises(AssistantError,match='变化'):
        catalog.configure(store,account_id='a',expected=saved['revision'],model='upstream-a',effort='low')


def test_removed_model_and_account_switch_during_fetch_do_not_silently_fall_back():
    catalog, broker, store, _, access = fixture()
    catalog.configure(store,account_id='a',expected='',model='upstream-b',effort='high')
    catalog.fetch=lambda access:parse_catalog({'models':[upstream('new-model')]})
    assert not catalog.status(store,refresh=True)['available']
    with pytest.raises(AssistantError,match='不可用'):
        catalog.resolve(store,access)
    def switch(access):
        broker.active='b'
        return parse_catalog({'models':[upstream('new-model')]})
    catalog.fetch=switch
    with pytest.raises(AssistantError,match='changed'):
        catalog.status(store,refresh=True)


def test_explicit_automatic_effort_survives_save_and_is_omitted_from_requests():
    from companion_codex.plan_web import request_body
    catalog, _, store, _, access = fixture()
    automatic = {**upstream("automatic"), "supported_reasoning_levels": [], "default_reasoning_level": None}
    catalog.fetch = lambda _: parse_catalog({"models": [automatic, upstream("adjustable")]})
    saved = catalog.configure(store, account_id="a", expected="", model="automatic", effort=None)
    assert saved["available"] and catalog.resolve(store, access) == ("automatic", None)
    assert "reasoning" not in request_body("example", {}, {"type": "string"}, "automatic", None)
    with pytest.raises(AssistantError, match="档位"):
        catalog.configure(store, account_id="a", expected=saved["revision"], model="automatic", effort="high")


def test_activity_nested_tasks_and_failure_clear_correctly():
    activity=Activity()
    with activity.track('bilibili','watching'):
        try:
            with activity.track('qq','replying'):
                assert [a['state'] for a in activity.snapshot()] == ['replying','watching']
                raise RuntimeError('synthetic')
        except RuntimeError:
            pass
        assert activity.snapshot()[0]['state'] == 'watching'
    assert activity.snapshot() == []
