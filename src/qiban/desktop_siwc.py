"""Isolated SIWC authorization validation. No inference or paid fallback exists here."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import jwt
from filelock import FileLock, Timeout

from companion_core.errors import AssistantError
from qiban.desktop_codex import DesktopCodex

ISSUER = 'https://auth.openai.com'
AUTHORIZE = ISSUER + '/api/accounts/authorize'
TOKEN = ISSUER + '/api/accounts/oauth/token'
RESOURCE = 'https://api.openai.com/v1'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
KEY = 'desktop-siwc-validation'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(url, form=None, *, empty=False):
    """Fixed trusted HTTPS hosts, bounded reads; never follow credential redirects."""
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'auth.openai.com'
            or parsed.port not in (None, 443) or parsed.username or parsed.password):
        raise ValueError('Untrusted authorization endpoint')
    data = urlencode(form).encode() if form is not None else None
    headers = {'Accept': 'application/json'}
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    with build_opener(NoRedirect()).open(Request(url, data=data, headers=headers), timeout=10) as response:
        raw = response.read(262145)
        if response.status != 200 or len(raw) > 262144:
            raise ValueError('Invalid authorization response')
        if empty:
            if raw.strip():
                raise ValueError('Invalid revocation response')
            return None
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Invalid authorization response')
        return result


def identity(token, client, nonce, jwks):
    """No token-supplied algorithms, URLs or unverified identity are trusted."""
    header = jwt.get_unverified_header(token)
    keys = [key for key in jwks['keys'] if key.get('kid') == header.get('kid')
            and key.get('kty') == 'RSA' and key.get('use', 'sig') == 'sig'
            and key.get('alg', 'RS256') == 'RS256']
    if header.get('alg') != 'RS256' or len(keys) != 1:
        raise ValueError('Invalid signing key')
    required = ['iss', 'aud', 'exp', 'iat', 'sub'] + (['nonce'] if nonce is not None else [])
    claims = jwt.decode(token, jwt.PyJWK.from_dict(keys[0]).key, algorithms=['RS256'],
        audience=client, issuer=ISSUER, options={'require': required})
    if (not isinstance(claims['sub'], str) or not claims['sub']
            or (nonce is not None and (not isinstance(claims['nonce'], str)
                                      or not hmac.compare_digest(claims['nonce'], nonce)))
            or (claims.get('azp') is not None and claims['azp'] != client)
            or (isinstance(claims['aud'], list) and len(claims['aud']) > 1 and claims.get('azp') != client)):
        raise ValueError('Invalid account identity')
    return claims


def callback(path, state, client):
    parsed = urlsplit(path)
    if parsed.path != '/auth/callback' or len(path) > 16000:
        raise ValueError('Invalid callback')
    values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
    if any(len(value) != 1 for value in values.values()):
        raise ValueError('Repeated callback field')
    values = {key: value[0] for key, value in values.items()}
    if not hmac.compare_digest(values.get('state', ''), state):
        raise ValueError('Invalid callback state')
    if 'error' in values:
        raise AssistantError('siwc_denied', '授权未完成，原有账号资料未改变。')
    issued = values.get('client_id', client)
    if (not issued or issued == 'dynamic_agent_client' or len(issued) > 256
            or not issued.isascii() or any(c.isspace() for c in issued)
            or (client and issued != client) or not values.get('code')):
        raise ValueError('Invalid registration callback')
    return values['code'], issued


class DesktopSiwc(DesktopCodex):
    """Validation route: one selected registration, persisted independently of Codex."""
    working_message = '正在处理栖伴的独立 ChatGPT 授权……'
    def __init__(self, root, store, *, transport=request, verify=identity, clock=time.time):
        super().__init__(root)
        self.store, self.transport, self.verify = store, transport, verify
        self.cleanup_unconfirmed = False
        self.clock = clock
        self.root.mkdir(parents=True, exist_ok=True)
        self.operation_lock = FileLock(str(self.root/'account-operation.lock'), timeout=0)

    def status(self):
        return {**super().status(), 'authentication_route': 'siwc_validation',
                'credential_store': 'encrypted_database_windows_key', 'automatic_renewal_enabled': False,
                'renew_on_explicit_check': True}

    def _load(self):
        with self.store.lock, self.store.db() as db:
            return self.store.state(KEY, db=db) or {}

    def _save(self, value):
        with self.lock, self.store.lock, self.store.db() as db:
            self._guard()
            self.store.set_state(KEY, value, db)

    def _retain_rotation(self, value):
        # After a request is dispatched, cancellation must not discard a rotated credential.
        with self.store.lock, self.store.db() as db:
            self.store.set_state(KEY, value, db)

    def _public(self, value):
        session = value.get('session')
        if not session:
            message = ('授权已失效，请重新登录 ChatGPT。' if value.get('renewal',{}).get('status') == 'reauthorization_required'
                       else '尚未登录，请使用 ChatGPT 账号登录。')
            self._set(state='signed_out', message=message)
            return
        permitted = 'chatgpt.tokens.use.direct' in session['scopes']
        expired = session['expires_at'] <= self.clock()
        message = 'ChatGPT 身份已验证；' + ('已获订阅使用权限。' if permitted else '尚未获订阅使用权限。')
        if expired:
            message += '当前凭证已到期。'
        renewal = value.get('renewal',{}).get('status')
        notices = {'pending':'上次续期中断，结果未确认，请重新登录。',
            'uncertain':'续期结果未确认，请重新登录；旧凭证不会重复续期。',
            'received':'已收到续期结果，身份校验尚未完成；请稍后检查登录状态。',
            'temporary_failure':'官方续期暂时不可用，授权已保留；请稍后检查登录状态。',
            'invalid_client':'应用注册配置未通过续期检查，请检查配置；重复登录不会自动解决。',
            'rejected':'续期请求未获接受，授权已保留，请检查授权状态。',
            'reauthorization_required':'当前授权无法续期，请重新登录。'}
        if renewal in notices:
            message += notices[renewal]
        self._set(state='authenticated', message=message, email=value.get('email', ''),
                  plan='订阅类型待核对', subscription_permission=permitted, token_expired=expired,
                  renewal_status=renewal)

    def _run(self, operation):
        self.cleanup_unconfirmed = False
        try:
            self._operate(operation)
        except AssistantError as error:
            messages = {'model_cancelled': '已取消账号操作；请重新检查当前登录状态。',
                        'model_timeout': '账号授权等待超时，请重新登录。',
                        'model_account_changed': '账号列表已更新，请重新选择账号。',
                        'model_tasks_stopping': '旧任务尚未全部退出，账号未切换；请稍后重新选择账号。',
                        'model_account_handoff_pending': '账号已保存，但任务恢复未完成；旧任务保持停止，请重新选择账号。',
                        'model_busy': '另一账号操作尚未结束，请稍后再试。',
                        'siwc_denied': '官方授权未完成，原有账号资料未改变。'}
            self._set(state='cancelled' if error.code == 'model_cancelled' else 'error',
                      code=error.code, message=messages.get(error.code, '授权验证未通过，原有账号资料未改变。'))
        except Exception:
            self._set(state='error', code='siwc_validation_failed',
                      message='官方授权验证未完成，请检查网络后重新尝试。原有账号资料未改变。')
        finally:
            with self.lock:
                self.url = None
                if self.cleanup_unconfirmed:
                    self.state['message'] += ' 未确认撤销本次未保存的远端授权，请在 ChatGPT 设置中检查 Qiban 授权。'
                    self.state['remote_revocation_confirmed'] = False

    def _metadata(self):
        value = self.transport(ISSUER + '/.well-known/openid-configuration')
        if value.get('issuer') != ISSUER or 'RS256' not in value.get('id_token_signing_alg_values_supported', []):
            raise ValueError('Invalid issuer metadata')
        # Each URL is independently constrained by request() before network access.
        return value

    def _operate(self, operation):
        deadline = time.monotonic()+30
        while True:
            self._guard()
            try:
                self.operation_lock.acquire(timeout=0)
                break
            except Timeout:
                if time.monotonic() > deadline:
                    raise AssistantError('model_busy','另一账号操作尚未结束') from None
                self.cancelled.wait(.05)
        try:
            self._operate_locked(operation)
        finally:
            self.operation_lock.release()

    def _verify_renewed_identity(self, token, value):
        metadata = self._metadata()
        try:
            claims = self.verify(token, value['client_id'], None, self.transport(metadata['jwks_uri']))
            nonce = value['session'].get('nonce')
            if claims['sub'] != value['subject'] or (nonce and 'nonce' in claims and claims['nonce'] != nonce):
                raise ValueError('Renewed identity mismatch')
        except jwt.InvalidTokenError:
            raise ValueError('Renewed identity invalid') from None

    def _operate_locked(self, operation):
        self._guard()
        value = self._load()
        if operation in ('refresh', 'platform_refresh'):
            from qiban.desktop_siwc_sessions import renew

            value = renew(value, write=self._retain_rotation, guard=self._guard, clock=self.clock,
                lead=self.platform_renewal_lead(value) if operation == 'platform_refresh' else 120,
                verify=self._verify_renewed_identity, exchange=lambda client, token:self.transport(TOKEN, {
                    'grant_type':'refresh_token', 'client_id':client, 'refresh_token':token, 'resource':RESOURCE}))
            self._public(value)
        elif operation == 'logout':
            self._logout(value)
        else:
            if not value.get('host'):
                value['host'] = 'urn:uuid:' + str(uuid.uuid4())
                self._save(value)
            self._login(value)

    def _login(self, value):
        state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
        result = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                try:
                    answer = callback(self.path, state, value.get('client_id'))
                except AssistantError as error:
                    result.append(error)
                    body = b'Authorization was declined. Return to Qiban.'
                except ValueError:
                    self.send_error(400, 'Invalid authorization callback')
                    return
                else:
                    result.append(answer)
                    body = b'Authorization received. Return to Qiban to check verification.'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass  # The consumed callback must not be exchanged twice after a browser disconnect.

        class Listener(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(.5)
                return connection, address

            def handle_error(self, request, client_address):
                pass  # Never emit callback path, code or token through server tracebacks.

        with Listener(('127.0.0.1', 0), Handler) as server:
            server.timeout = .2
            redirect = f'http://127.0.0.1:{server.server_port}/auth/callback'
            parameters = {'client_id': value.get('client_id', 'dynamic_agent_client'),
                'ext_agent_host_id': value['host'], 'response_type': 'code', 'redirect_uri': redirect,
                'scope': SCOPES, 'resource': RESOURCE, 'state': state, 'nonce': nonce,
                'code_challenge_method': 'S256', 'code_challenge': base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')}
            if not value.get('client_id'):
                parameters['agent_name_hint'] = 'Qiban'
            with self.lock:
                self._guard()
                self.url = AUTHORIZE + '?' + urlencode(parameters)
                self.state = {'state': 'waiting', 'message': '请在官方页面授权 Qiban；本次仅验证登录，不发送模型请求。'}
            deadline = time.monotonic() + 600
            while not result:
                self._guard()
                if time.monotonic() > deadline:
                    raise AssistantError('model_timeout', 'Authorization timeout')
                server.handle_request()
        with self.lock:
            self.url = None
        self._guard()
        if isinstance(result[0], Exception):
            raise result[0]
        code, client = result[0]
        tokens = self.transport(TOKEN, {'grant_type': 'authorization_code', 'client_id': client,
            'code': code, 'code_verifier': verifier, 'redirect_uri': redirect, 'resource': RESOURCE})
        try:
            self._guard()
            metadata = self._metadata()
            claims = self.verify(tokens['id_token'], client, nonce, self.transport(metadata['jwks_uri']))
            if value.get('subject') and claims['sub'] != value['subject']:
                raise ValueError('Returning account mismatch')
            if (tokens.get('token_type', '').lower() != 'bearer'
                    or not isinstance(tokens.get('access_token'), str) or not tokens['access_token']
                    or not isinstance(tokens.get('scope'), str)
                    or type(tokens.get('expires_in')) is not int or not 0 < tokens['expires_in'] <= 86400
                    or ('offline_access' in tokens['scope'].split() and not tokens.get('refresh_token'))):
                raise ValueError('Invalid credential response')
            saved = {**value, 'renewal':{}, 'client_id': client, 'subject': claims['sub'], 'email': claims.get('email', ''),
                'session': {key: tokens[key] for key in ('access_token', 'id_token', 'token_type')}}
            saved['session'].update(refresh_token=tokens.get('refresh_token'), scopes=tokens['scope'].split(),
                                    nonce=nonce, expires_at=self.clock() + tokens['expires_in'])
            self._save(saved)
        except Exception as error:
            # Failed or cancelled attempts never become active. Revoke an unused renewable session.
            # A committed new account remains valid if only task handoff failed.
            committed = isinstance(error, AssistantError) and error.code == 'model_account_handoff_pending'
            if tokens.get('refresh_token') and not committed:
                try:
                    self._revoke(client, tokens['refresh_token'])
                except Exception:
                    self.cleanup_unconfirmed = True
            raise
        self._public(saved)

    def _revoke(self, client, token):
        endpoint = self._metadata()['revocation_endpoint']
        for attempt in range(2):
            try:
                self.transport(endpoint, {'client_id': client, 'token': token,
                    'token_type_hint': 'refresh_token'}, empty=True)
                return
            except (HTTPError, OSError) as error:
                if attempt or (isinstance(error, HTTPError) and error.code < 500):
                    raise
                time.sleep(.3)

    def _logout(self, value):
        session = value.get('session', {})
        pending = value.get('renewal', {})
        if pending.get('status') in ('received', 'uncertain'):
            received = pending.get('tokens', {}).get('refresh_token')
            if isinstance(received, str) and received:
                session = {**session, 'refresh_token':received}
        confirmed = not bool(session)
        if session.get('refresh_token'):
            try:
                self._revoke(value['client_id'], session['refresh_token'])
                confirmed = True
            except Exception:
                confirmed = False
        # Once revocation was requested, never retain a possibly revoked local session on cancellation.
        self._retain_rotation({key: item for key, item in value.items() if key not in ('session','renewal')})
        self._public({})
        if not confirmed:
            self._set(state='signed_out', remote_revocation_confirmed=False,
                      message='本机账号已退出，但未确认远端撤销。请在 ChatGPT 设置中断开 Qiban 授权。')
