from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from bili_assistant.api import TOOLS, call
from companion_core.errors import AssistantError


def test_reply_grant_requires_explicit_boolean_scope_and_version():
    validate = Draft202012Validator(TOOLS["reply_authorization_set"][1])
    args = {"scope_id": "synthetic", "enabled": True, "expected_version": None}
    assert not list(validate.iter_errors(args))
    for invalid in (args | {"enabled": "true"}, args | {"nickname": "family"},
                    {k: v for k, v in args.items() if k != "expected_version"}):
        assert list(validate.iter_errors(invalid))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["reply_authorization_list", "reply_authorization_set", "reply_policy_list", "reply_policy_set"])
async def test_legacy_api_cannot_bypass_authenticated_service(name):
    with pytest.raises(AssistantError) as rejected:
        await call(SimpleNamespace(store=SimpleNamespace(backend="sqlite")), name, {})
    assert rejected.value.code == "service_entry_required"
