"""Rotating-session recovery. Call under the installation's account-operation lock."""
import json
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError

TERMINAL = frozenset({'invalid_grant', 'invalid_refresh_token', 'token_expired',
                     'refresh_token_expired', 'refresh_token_invalidated', 'refresh_token_reused'})


def retry_time(error, now):
    hint = (error.headers or {}).get('Retry-After', '')
    try:
        delay = int(hint) if hint.isdigit() and len(hint) < 12 else parsedate_to_datetime(hint).timestamp()-now
    except (ValueError, TypeError, AttributeError, OverflowError):
        delay = 60
    return now+max(60, delay)


def error_code(error):
    try:
        body = json.loads(error.read(16384))
        value = body.get('error')
        code = value.get('code') if isinstance(value, dict) else value
        return code if code in TERMINAL | {'invalid_client'} else 'unclassified'
    except (ValueError, TypeError, AttributeError):
        return 'unclassified'


def replacement(tokens, previous, now):
    """OAuth permits omission of unchanged scope and ID token in refresh responses."""
    scope = tokens.get('scope', ' '.join(previous['scopes']))
    if (not isinstance(scope, str) or tokens.get('token_type', '').lower() != 'bearer'
            or any(not isinstance(tokens.get(key), str) or not tokens[key]
                   for key in ('access_token', 'refresh_token'))
            or type(tokens.get('expires_in')) is not int or not 0 < tokens['expires_in'] <= 86400
            or ('id_token' in tokens and (not isinstance(tokens['id_token'], str) or not tokens['id_token']))):
        raise ValueError('Invalid renewed session')
    return {**previous, 'access_token':tokens['access_token'], 'refresh_token':tokens['refresh_token'],
            'id_token':tokens.get('id_token', previous.get('id_token')), 'scopes':scope.split(),
            'expires_at':now+tokens['expires_in'], 'token_type':'Bearer'}


def renew(value, *, write, exchange, verify, guard, clock):
    """One attempt; persist dispatch and receipt before publishing a replacement.

    Cancellation is honored before dispatch. After dispatch, retaining a verified
    replacement takes precedence over cancellation, since the old token may be spent.
    A stored receipt can finish validation after restart without replaying refresh.
    """
    session = value.get('session')
    if not session:
        return value
    status = value.get('renewal', {})
    if status.get('status') in ('pending', 'uncertain', 'invalid_client', 'rejected'):
        return value
    if status.get('status') != 'received':
        if session['expires_at'] > clock()+120 or status.get('retry_at', 0) > clock():
            return value
        if not session.get('refresh_token'):
            value = {**value, 'renewal':{'status':'reauthorization_required'}}
            write(value)
            return value
        guard()
        value = {**value, 'renewal':{'status':'pending'}}
        write(value)  # If this write fails, no request is sent.
        try:
            tokens = exchange(value['client_id'], session['refresh_token'])
        except HTTPError as error:
            code = error_code(error)
            if code in TERMINAL:
                value = {key:item for key,item in value.items() if key != 'session'}
                value['renewal'] = {'status':'reauthorization_required', 'error':code}
            elif code == 'invalid_client':
                value['renewal'] = {'status':'invalid_client', 'error':code}
            elif error.code == 429 or error.code >= 500:
                value['renewal'] = {'status':'temporary_failure', 'retry_at':retry_time(error, clock()),
                                    'http_status':error.code}
            else:
                value['renewal'] = {'status':'rejected', 'http_status':error.code}
            write(value)
            return value
        except (OSError, ValueError):
            # Transport loss does not prove whether the rotating token was consumed.
            value['renewal'] = {'status':'uncertain'}
            write(value)
            return value
        value = {**value, 'renewal':{'status':'received', 'tokens':tokens, 'received_at':clock()}}
        write(value)  # A failed write leaves pending on disk; never replay the old token.
    tokens = value['renewal']['tokens']
    try:
        updated = replacement(tokens, session, value['renewal']['received_at'])
        if 'id_token' in tokens:
            verify(tokens['id_token'], value)
    except (HTTPError, OSError):
        # Keep the encrypted receipt until signature metadata can be retrieved.
        return value
    except ValueError:
        value = {**value, 'renewal':{**value['renewal'], 'status':'uncertain'}}
        write(value)
        return value
    value = {**value, 'session':updated, 'renewal':{'status':'complete', 'completed_at':clock()}}
    write(value)
    return value
