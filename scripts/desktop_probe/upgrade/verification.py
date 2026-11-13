"""The Windows-verification channel a rollback must pass through, installed by the host only.

A rollback of a confirmed upgrade changes which data generation is live, so it must be gated by
the same Windows identity verification the recovery flows use. That verification belongs to the
native host: this backend cannot raise a Windows prompt, and it must not accept anything a page,
a command line switch or a writable file could forge.

So the contract is an in-process channel. The native host installs one callable with
``install(handler)``; the handler performs the Windows check through the host's existing
verification code and returns the receipt for the operation it just authorised. Until a host
installs one, every rollback verb refuses with ``upgrade_rollback_unavailable`` - which is the
state of this stage. Nothing here claims that host authorisation exists yet, and no JSON file in
the data root can stand in for it: an earlier draft read a receipt file, which was writable by
the same person and replayable, and that is deliberately gone.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from companion_core.errors import AssistantError

ACTION = 'rollback'
FIELDS = frozenset({'session', 'at'})
MAX_AGE_SECONDS = 900
UNAVAILABLE = ('upgrade_rollback_unavailable',
               '回退入口还没有接入宿主机的 Windows 身份验证通道，本阶段不可调用。')
INVALID = ('upgrade_verification_invalid', '这次的 Windows 身份验证结果无效，未继续回退。')

_channel = None


def install(handler):
    """The trusted host installs the callable that performs Windows verification."""
    if not callable(handler):
        raise AssistantError(*INVALID)
    global _channel
    _channel = handler
    return handler


def clear():
    global _channel
    _channel = None


def installed():
    return _channel is not None


def authorize(record):
    """Verify through the installed channel and accept only a receipt for this transaction."""
    if _channel is None:
        raise AssistantError(*UNAVAILABLE)
    request = {'installation': record['installation'], 'transaction': record['id'],
               'action': ACTION}
    try:
        receipt = _channel(request)
    except AssistantError:
        raise
    except Exception:
        raise AssistantError(*INVALID) from None
    if not isinstance(receipt, dict) or set(receipt) != FIELDS:
        raise AssistantError(*INVALID)
    if not isinstance(receipt['session'], str) or not receipt['session']:
        raise AssistantError(*INVALID)
    try:
        taken = datetime.fromisoformat(receipt['at'])
    except (TypeError, ValueError):
        raise AssistantError(*INVALID) from None
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=timezone.utc)
    age = time.time()-taken.timestamp()
    if age < 0 or age > MAX_AGE_SECONDS:
        raise AssistantError('upgrade_verification_expired',
                             '这次的 Windows 身份验证已过期，请重新确认后再回退。')
    return {'session': receipt['session'], 'at': receipt['at']}
