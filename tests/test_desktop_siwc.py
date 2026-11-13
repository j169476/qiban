"""Real loopback and RSA verification, synthetic credentials, no external account access."""
import copy
import json
import threading
import time
from contextlib import nullcontext
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from companion_core.errors import AssistantError
from qiban.desktop_siwc import ISSUER, KEY, TOKEN, DesktopSiwc, callback, identity, request


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.value = {}

    def db(self):
        return nullcontext(None)

    def state(self, key, db=None):
        assert key == KEY
        return copy.deepcopy(self.value)

    def set_state(self, key, value, db=None):
        assert key == KEY
        self.value = copy.deepcopy(value)


@pytest.fixture
def signing():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return key, {'keys': [{**public, 'kid': 'synthetic-key', 'use': 'sig', 'alg': 'RS256'}]}


def token(signing, **claims):
    return jwt.encode({'iss': ISSUER, 'aud': 'oaiapp_synthetic', 'sub': 'synthetic-subject',
        'iat': int(time.time()), 'exp': int(time.time()) + 3600, 'nonce': 'test-nonce',
        'email': 'synthetic@example.invalid', **claims}, signing[0], algorithm='RS256', headers={'kid': 'synthetic-key'})


@pytest.mark.parametrize('claims', [dict(iss='https://wrong.invalid'), dict(aud='other'),
    dict(exp=1), dict(nonce='wrong'), dict(sub=''), dict(azp='other'), dict(aud=['oaiapp_synthetic','other'])])
def test_identity_rejects_wrong_identity(signing, claims):
    with pytest.raises((ValueError, jwt.InvalidTokenError)):
        identity(token(signing, **claims), 'oaiapp_synthetic', 'test-nonce', signing[1])


def test_identity_rejects_unsigned_or_forged_tokens(signing):
    unsigned = jwt.encode({'sub': 'forged'}, key='', algorithm='none')
    with pytest.raises(ValueError):
        identity(unsigned, 'oaiapp_synthetic', 'test-nonce', signing[1])
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        identity(token((other, signing[1])), 'oaiapp_synthetic', 'test-nonce', signing[1])


@pytest.mark.parametrize('query', ['state=wrong&code=x&client_id=oaiapp_synthetic',
    'state=s&state=s&code=x&client_id=oaiapp_synthetic', 'state=s&code=x',
    'state=s&code=x&client_id=dynamic_agent_client'])
def test_callback_rejects_incomplete_or_forged_registration(query):
    with pytest.raises(ValueError):
        callback('/auth/callback?' + query, 's', None)


def test_returning_registration_and_denial():
    assert callback('/auth/callback?state=s&code=x', 's', 'saved') == ('x', 'saved')
    with pytest.raises(ValueError):
        callback('/auth/callback?state=s&code=x&client_id=other', 's', 'saved')
    with pytest.raises(AssistantError) as error:
        callback('/auth/callback?state=s&error=access_denied', 's', None)
    assert error.value.code == 'siwc_denied'


def wait(value, state=None):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = value.status()
        if (state and result['state'] == state) or (not state and not result['busy']):
            return result
        time.sleep(.01)
    raise AssertionError('Broker did not settle')


def flow(tmp_path, signing, *, scope='openid offline_access chatgpt.tokens.use.direct', saved=None, cancel_after_exchange=False):
    store, calls, parameters = Store(), [], {}
    if saved:
        store.value = copy.deepcopy(saved)
    def transport(url, form=None, **kwargs):
        calls.append((url, form, kwargs))
        if url.endswith('openid-configuration'):
            return {'issuer': ISSUER, 'id_token_signing_alg_values_supported': ['RS256'],
                'jwks_uri': ISSUER + '/keys', 'revocation_endpoint': ISSUER + '/revoke'}
        if url.endswith('/keys'):
            return signing[1]
        if url.endswith('/revoke'):
            return None
        assert url == TOKEN
        assert form['client_id'] == 'oaiapp_synthetic' and form['redirect_uri'] == parameters['redirect_uri'][0]
        assert form['code'] == 'synthetic-code' and form['resource'] == 'https://api.openai.com/v1'
        if cancel_after_exchange:
            value.cancel()
        return {'id_token': token(signing, nonce=parameters['nonce'][0]), 'access_token': 'SYNTHETIC-ACCESS',
                'refresh_token': 'SYNTHETIC-REFRESH', 'expires_in': 3600, 'token_type': 'Bearer', 'scope': scope}
    value = DesktopSiwc(tmp_path, store, transport=transport)
    value.begin('login')
    wait(value, 'waiting')
    parameters.update(parse_qs(urlsplit(value.open_login()['url']).query))
    return value, store, calls, parameters


def complete(value, parameters, **overrides):
    query = {'state': parameters['state'][0], 'code': 'synthetic-code', 'client_id': 'oaiapp_synthetic', **overrides}
    with urlopen(parameters['redirect_uri'][0] + '?' + urlencode(query), timeout=2) as response:
        assert response.status == 200
    return wait(value)


def test_real_loopback_authentication_and_restart_keep_tokens_private(tmp_path, signing):
    value, store, calls, parameters = flow(tmp_path, signing)
    # A forged callback does not consume the pending real callback.
    with pytest.raises(HTTPError):
        urlopen(parameters['redirect_uri'][0] + '?state=wrong&code=forged', timeout=2)
    assert value.status()['state'] == 'waiting'
    result = complete(value, parameters)
    assert result['state'] == 'authenticated' and result['subscription_permission']
    assert result['model_calls_enabled'] is False
    assert 'SYNTHETIC-' not in json.dumps(result)
    assert len([call for call in calls if call[0] == TOKEN]) == 1
    restarted = DesktopSiwc(tmp_path, store, transport=lambda *a: pytest.fail('Local status must not call network'))
    restarted.begin('refresh')
    assert wait(restarted)['state'] == 'authenticated'
    assert not list(tmp_path.rglob('auth.json'))
    value.begin('logout')
    assert wait(value)['state'] == 'signed_out'
    assert 'session' not in store.value and store.value['client_id'] == 'oaiapp_synthetic'
    value.close()


def test_identity_only_authorization_never_enables_inference(tmp_path, signing):
    value, store, calls, parameters = flow(tmp_path, signing, scope='openid')
    result = complete(value, parameters)
    assert result['state'] == 'authenticated' and not result['subscription_permission']
    assert not result['model_calls_enabled']


def test_cancel_closes_listener_and_preserves_active_credentials(tmp_path, signing):
    saved = {'host': 'stable-host', 'client_id': 'oaiapp_synthetic', 'subject': 'synthetic-subject',
             'session': {'access_token': 'existing'}}
    value, store, calls, parameters = flow(tmp_path, signing, saved=saved)
    assert parameters['client_id'] == ['oaiapp_synthetic'] and 'agent_name_hint' not in parameters
    assert value.cancel()['cancel_requested']
    assert wait(value)['state'] == 'cancelled' and store.value == saved and not calls
    with pytest.raises(OSError):
        urlopen(parameters['redirect_uri'][0], timeout=.5)


def test_cancel_after_exchange_cannot_save_credentials(tmp_path, signing):
    value, store, calls, parameters = flow(tmp_path, signing, cancel_after_exchange=True)
    assert complete(value, parameters)['state'] == 'cancelled'
    assert 'session' not in store.value
    assert any(url.endswith('/revoke') for url, _, _ in calls)


def test_declined_authorization_never_exchanges_code(tmp_path, signing):
    value, store, calls, parameters = flow(tmp_path, signing)
    result = complete(value, parameters, error='access_denied')
    assert result['code'] == 'siwc_denied' and not calls and 'session' not in store.value


def test_cancel_cleanup_failure_is_visible_without_saving_tokens(tmp_path, signing):
    value, store, calls, parameters = flow(tmp_path, signing, cancel_after_exchange=True)
    original = value.transport
    def disconnected(url, *args, **kwargs):
        if url.endswith('/revoke'):
            raise OSError('private-remote-failure')
        return original(url, *args, **kwargs)
    value.transport = disconnected
    result = complete(value, parameters)
    assert result['state'] == 'cancelled' and result['remote_revocation_confirmed'] is False
    assert 'ChatGPT 设置' in result['message'] and 'private-remote' not in str(result)
    assert 'session' not in store.value


def test_returning_different_identity_is_not_committed(tmp_path, signing):
    saved = {'host': 'stable-host', 'client_id': 'oaiapp_synthetic', 'subject': 'original-subject'}
    value, store, calls, parameters = flow(tmp_path, signing, saved=saved)
    assert complete(value, parameters)['state'] == 'error'
    assert store.value == saved
    assert any(url.endswith('/revoke') for url, _, _ in calls)


def test_logout_remote_failure_is_explicit_and_local_session_removed(tmp_path):
    store = Store()
    store.value = {'host': 'stable', 'client_id': 'saved', 'session': {'refresh_token': 'secret'}}
    def offline(*args):
        raise OSError('private-network-details')
    value = DesktopSiwc(tmp_path, store, transport=offline)
    value.begin('logout')
    result = wait(value)
    assert result['state'] == 'signed_out' and result['remote_revocation_confirmed'] is False
    assert 'session' not in store.value and 'private-network' not in json.dumps(result)


@pytest.mark.parametrize('url', ['http://auth.openai.com/x', 'https://auth.openai.com.evil/x',
    'https://user@auth.openai.com/x', 'https://auth.openai.com:444/x'])
def test_credentials_cannot_be_posted_to_other_origins(url):
    with pytest.raises(ValueError):
        request(url, {'token': 'synthetic'})
