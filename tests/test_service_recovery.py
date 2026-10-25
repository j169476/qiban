import secrets

import pytest

from companion_core.errors import AssistantError
from companion_service.recovery import unwrap, wrap


def test_wrapped_recovery_key_requires_separate_password_and_authenticates_bytes():
    key = secrets.token_bytes(32)
    password = "synthetic-recovery-password-2026"
    blob = wrap(key, password)
    assert key not in blob
    assert unwrap(blob, password) == key
    with pytest.raises(AssistantError):
        unwrap(blob, "synthetic-wrong-password")
    with pytest.raises(AssistantError):
        unwrap(blob[:-1] + bytes([blob[-1] ^ 1]), password)
    with pytest.raises(AssistantError):
        wrap(key, "short")
