"""Synthetic rotating-token failures and restart recovery; no real account traffic."""
import copy
import io
import json
import threading
import time
from urllib.error import HTTPError

import jwt
import pytest
from test_desktop_siwc import Store, wait
from test_desktop_siwc import signing as signing

from qiban.desktop_siwc import ISSUER, TOKEN, DesktopSiwc
from qiban.desktop_siwc_sessions import TERMINAL, renew


def initial():
    return {'host':'host', 'client_id':'client', 'subject':'subject', 'email':'test@example.invalid',
        'session':{'access_token':'old-access', 'refresh_token':'old-refresh', 'id_token':'old-id',
                   'scopes':['openid','offline_access','chatgpt.tokens.use.direct'], 'expires_at':1001}}


def fresh(**values):
    return {'access_token':'new-access', 'refresh_token':'new-refresh',
            'token_type':'Bearer', 'expires_in':3600, **values}


def http(code, status=400):
    return HTTPError('https://auth.openai.com/token', status, 'private-error', {},
                     io.BytesIO(json.dumps({'error':code}).encode()))


class Disk:
    def __init__(self, value=None):
        self.value = value or initial()
        self.calls = 0

    def write(self, value):
        self.value = copy.deepcopy(value)

    def run(self, exchange=None, verify=None, guard=None, now=1000):
        def send(*args):
            self.calls += 1
            return exchange(*args) if exchange else fresh()
        return renew(copy.deepcopy(self.value), write=self.write, exchange=send,
                     verify=verify or (lambda *a:None), guard=guard or (lambda:None), clock=lambda:now)


def test_rotation_atomically_replaces_pair_and_never_sends_scope():
    disk = Disk()
    def exchange(client, token):
        assert (client,token) == ('client','old-refresh')
        assert disk.value['renewal']['status'] == 'pending'
        return fresh()
    result = disk.run(exchange)
    assert result['session']['access_token'] == 'new-access'
    assert result['session']['refresh_token'] == 'new-refresh'
    assert result['session']['scopes'] == initial()['session']['scopes']
    assert result['renewal']['status'] == 'complete'
    assert disk.run() == result and disk.calls == 1


@pytest.mark.parametrize('code', sorted(TERMINAL))
def test_confirmed_invalid_tokens_cleared_but_registration_retained(code):
    disk = Disk()
    def fail(*args):
        raise http(code)
    result = disk.run(fail)
    assert 'session' not in result and result['client_id'] == 'client' and result['host'] == 'host'
    assert result['renewal']['status'] == 'reauthorization_required'
    disk.run()
    assert disk.calls == 1


@pytest.mark.parametrize('status', [429,500,503])
def test_server_temporary_error_retains_session_and_enforces_delay(status):
    disk = Disk()
    def fail(*args):
        raise http('temporary',status)
    result = disk.run(fail)
    assert result['session'] == initial()['session'] and result['renewal']['retry_at'] == 1060
    disk.run(now=1059)
    assert disk.calls == 1
    assert disk.run(now=1060)['renewal']['status'] == 'complete'
    assert disk.calls == 2


def test_invalid_client_stops_retry_without_erasing_tokens():
    disk = Disk()
    def fail(*args):
        raise http('invalid_client')
    result = disk.run(fail)
    assert result['session'] == initial()['session'] and result['renewal']['status'] == 'invalid_client'
    disk.run(now=9000)
    assert disk.calls == 1


def test_lost_response_preserves_tokens_but_never_replays_rotation():
    disk = Disk()
    def lost(*args):
        raise OSError('SYNTHETIC-PRIVATE-ERROR')
    result = disk.run(lost)
    assert result['session'] == initial()['session'] and result['renewal']['status'] == 'uncertain'
    assert 'SYNTHETIC-PRIVATE' not in str(result)
    restarted = Disk(copy.deepcopy(disk.value))
    restarted.run(now=10000)
    assert restarted.calls == 0


def test_process_death_during_post_leaves_no_replay_on_restart():
    disk = Disk()
    class Crash(BaseException):
        pass
    def crash(*args):
        raise Crash()
    with pytest.raises(Crash):
        disk.run(crash)
    assert disk.value['renewal']['status'] == 'pending'
    restarted = Disk(copy.deepcopy(disk.value))
    restarted.run()
    assert restarted.calls == 0


def test_received_response_survives_signature_network_error_and_restart():
    disk = Disk()
    def offline(*args):
        raise OSError('Cannot reach keys')
    result = disk.run(lambda *a:fresh(id_token='new-id'), verify=offline)
    assert result['renewal']['status'] == 'received'
    restarted = Disk(copy.deepcopy(disk.value))
    verified = []
    result = restarted.run(verify=lambda token, value:verified.append((token,value['subject'])))
    assert result['renewal']['status'] == 'complete' and restarted.calls == 0
    assert verified == [('new-id','subject')]


def test_invalid_signature_cannot_replace_active_pair():
    disk = Disk()
    def invalid(*args):
        raise ValueError('Signature invalid')
    result = disk.run(lambda *a:fresh(id_token='forged'), verify=invalid)
    assert result['session'] == initial()['session'] and result['renewal']['status'] == 'uncertain'


def test_crash_after_receipt_before_commit_resumes_without_new_request():
    disk = Disk()
    original = disk.write
    def failing_write(value):
        if value['renewal']['status'] == 'complete':
            raise OSError('Injected commit failure')
        original(value)
    disk.write = failing_write
    with pytest.raises(OSError):
        disk.run()
    assert disk.value['renewal']['status'] == 'received'
    restarted = Disk(copy.deepcopy(disk.value))
    assert restarted.run()['session']['refresh_token'] == 'new-refresh'
    assert restarted.calls == 0


def test_cancel_before_dispatch_does_not_write_or_request():
    disk = Disk()
    def cancel():
        raise RuntimeError('Cancelled')
    with pytest.raises(RuntimeError):
        disk.run(guard=cancel)
    assert disk.value == initial() and disk.calls == 0


def test_cancellation_after_dispatch_keeps_replacement(tmp_path):
    store = Store()
    store.value = initial()
    def transport(url, form=None, **kwargs):
        assert form == {'grant_type':'refresh_token','client_id':'client','refresh_token':'old-refresh',
                        'resource':'https://api.openai.com/v1'}
        broker.cancel()
        return fresh()
    broker = DesktopSiwc(tmp_path,store,transport=transport,clock=lambda:1000)
    broker.begin('refresh')
    result = wait(broker)
    assert result['state'] == 'authenticated' and result['renewal_status'] == 'complete'
    assert store.value['session']['refresh_token'] == 'new-refresh'


def test_account_lock_serializes_two_brokers_and_reloads_latest_session(tmp_path):
    store = Store()
    store.value = initial()
    entered,release = threading.Event(),threading.Event()
    calls = []
    def transport(url,form=None):
        calls.append(form['refresh_token'])
        entered.set()
        assert release.wait(2)
        return fresh()
    first = DesktopSiwc(tmp_path,store,transport=transport,clock=lambda:1000)
    second = DesktopSiwc(tmp_path,store,transport=transport,clock=lambda:1000)
    first.begin('refresh')
    assert entered.wait(2)
    second.begin('refresh')
    time.sleep(.1)
    release.set()
    assert wait(first)['state'] == wait(second)['state'] == 'authenticated'
    assert calls == ['old-refresh']


def test_logout_revokes_received_replacement_and_erases_journal(tmp_path):
    store = Store()
    store.value = {**initial(), 'renewal':{'status':'received','tokens':fresh()}}
    broker = DesktopSiwc(tmp_path,store,clock=lambda:1000)
    revoked = []
    broker._revoke = lambda client,token:revoked.append(token)
    broker.begin('logout')
    assert wait(broker)['state'] == 'signed_out' and revoked == ['new-refresh']
    assert 'session' not in store.value and 'renewal' not in store.value


@pytest.mark.parametrize('claims', [{}, {'sub':'different'}, {'iss':'https://wrong.invalid'},
                                  {'aud':'other-client'}, {'exp':1}])
def test_real_rsa_refresh_identity_without_nonce(tmp_path, signing, claims):
    store = Store()
    store.value = initial()
    signed = jwt.encode({'iss':ISSUER, 'sub':'subject', 'aud':'client', 'iat':int(time.time()),
                         'exp':int(time.time())+3600, **claims}, signing[0],
                        algorithm='RS256', headers={'kid':'synthetic-key'})
    def transport(url, form=None):
        if url == TOKEN:
            return fresh(id_token=signed)
        if url.endswith('openid-configuration'):
            return {'issuer':ISSUER,'id_token_signing_alg_values_supported':['RS256'],'jwks_uri':ISSUER+'/keys'}
        return signing[1]
    broker = DesktopSiwc(tmp_path,store,transport=transport,clock=lambda:1000)
    broker.begin('refresh')
    result = wait(broker)
    assert result['renewal_status'] == ('uncertain' if claims else 'complete')
    assert store.value['session']['refresh_token'] == ('old-refresh' if claims else 'new-refresh')
    assert signed not in json.dumps(result)


def test_server_retry_after_is_honored():
    disk = Disk()
    def fail(*args):
        error = http('temporary',429)
        error.headers = {'Retry-After':'300'}
        raise error
    assert disk.run(fail)['renewal']['retry_at'] == 1300
    disk.run(now=1299)
    assert disk.calls == 1
