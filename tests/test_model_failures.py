"""Terminal failure handling without a provider, credentials, or model calls."""

import asyncio
import json

import pytest
from test_isolated_codex import fake_rpc

from companion_codex.app_server import TurnEvidence
from companion_codex.failures import turn_failure
from companion_core.errors import AssistantError


@pytest.mark.parametrize(
    "kind,code",
    [
        ("usageLimitExceeded", "model_usage_limit"),
        ("rateLimitExceeded", "model_rate_limit"),
        ("unauthorized", "model_auth"),
        ("modelNotFound", "model_unavailable"),
        ("contextWindowExceeded", "model_input_size"),
        ("sessionBudgetExceeded", "model_budget"),
        ("badRequest", "model_request_invalid"),
        ("serverOverloaded", "model_temporary"),
        ("internalServerError", "model_temporary"),
        ("flexUnavailable", "model_temporary"),
        ("cyberPolicy", "model_permission"),
        ("misalignmentPolicyViolation", "model_permission"),
    ],
)
def test_terminal_failure_rejects_partial_answer_and_exposes_only_fixed_diagnostics(kind, code):
    evidence = TurnEvidence()
    evidence.thread_id, evidence.turn_id = "thread", "turn"
    evidence.update(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "item": {"id": "partial", "type": "agentMessage", "text": '{"unfinished":true}'},
            },
        }
    )
    with pytest.raises(AssistantError) as caught:
        evidence.update(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread",
                    "turn": {
                        "id": "turn",
                        "status": "failed",
                        "error": {
                            "codexErrorInfo": kind,
                            "message": "PRIVATE-ACCESS-TOKEN",
                            "additionalDetails": "private@example.invalid",
                        },
                    },
                },
            }
        )
    assert caught.value.code == code and not evidence.completed
    public = json.dumps(caught.value.as_dict())
    assert "PRIVATE" not in public and "private@" not in public
    assert caught.value.diagnostics == {"completion_status": "failed", "codex_error": kind}


@pytest.mark.parametrize(
    "http,expected",
    [
        (400, "model_request_invalid"),
        (401, "model_auth"),
        (403, "model_permission"),
        (429, "model_rate_limit"),
        (500, "model_temporary"),
        (503, "model_temporary"),
        (404, "model_failed"),
        (None, "model_failed"),
        (True, "model_failed"),
        ("401", "model_failed"),
        (999, "model_failed"),
    ],
)
def test_http_status_is_not_a_claim_about_subscription_balance(http, expected):
    failure = turn_failure(
        "failed", {"codexErrorInfo": {"responseStreamConnectionFailed": {"httpStatusCode": http}}}
    )
    assert failure.code == expected
    assert (
        "http_status" in failure.diagnostics
        if type(http) is int and 100 <= http <= 599
        else "http_status" not in failure.diagnostics
    )


@pytest.mark.parametrize(
    "error",
    [
        None,
        [],
        "PRIVATE",
        {"message": "subscription_sharing_usage_limit_exceeded"},
        {"codexErrorInfo": "unknown-secret", "additionalDetails": '{"error":{"code":"unauthorized"}}'},
        {"codexErrorInfo": {"unknown-secret": {"httpStatusCode": 401}}},
        {"codexErrorInfo": {"httpConnectionFailed": None}},
    ],
)
def test_unknown_error_text_never_drives_account_or_billing_decisions(error):
    failure = turn_failure("failed", error)
    assert failure.code == "model_failed"
    assert failure.diagnostics == {"completion_status": "failed"}


def test_interrupted_is_not_a_quota_error_or_success():
    error = turn_failure("interrupted", {"codexErrorInfo": "usageLimitExceeded"})
    assert error.code == "model_interrupted"
    assert error.diagnostics == {"completion_status": "interrupted"}


def test_mismatched_terminal_event_rejected_before_failure_classification():
    evidence = TurnEvidence()
    evidence.thread_id, evidence.turn_id = "thread", "turn"
    with pytest.raises(AssistantError) as caught:
        evidence.update(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "other",
                    "turn": {
                        "id": "turn",
                        "status": "failed",
                        "error": {"codexErrorInfo": "usageLimitExceeded"},
                    },
                },
            }
        )
    assert caught.value.code == "model_protocol"


def test_success_still_requires_completed_terminal_event():
    evidence = TurnEvidence()
    assert not evidence.completed
    evidence.update({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
    assert evidence.completed


@pytest.mark.parametrize("during_start", [False, True])
async def test_real_child_terminal_failure_propagates_and_cleanup_reaps(tmp_path, during_start):
    # A Python child emits the protocol; Codex and the network are never started.
    script = """import sys,json,time
request=json.loads(sys.stdin.readline())
rows=[{'id':request['id'],'result':{'turn':{'id':'turn'}}},
      {'method':'turn/completed','params':{'threadId':'thread','turn':{
       'id':'turn','status':'failed','error':{'codexErrorInfo':'usageLimitExceeded',
       'message':'SYNTHETIC-PRIVATE-TOKEN'}}}}]
for row in ORDER: print(json.dumps(row),flush=True)
time.sleep(60)
""".replace("ORDER", "reversed(rows)" if during_start else "rows")
    rpc = fake_rpc(tmp_path, script)
    evidence = TurnEvidence()
    evidence.thread_id = "thread"
    try:
        with pytest.raises(AssistantError) as caught:
            started = await rpc.call("turn/start", {}, evidence.update)
            evidence.turn_id = started["turn"]["id"]
            while not evidence.completed:
                evidence.update(await rpc.receive())
        assert caught.value.code == "model_usage_limit"
        assert "SYNTHETIC" not in json.dumps(caught.value.as_dict())
        assert rpc.counter == 1 and not evidence.completed
    finally:
        await asyncio.to_thread(rpc.close)
    assert rpc.process.poll() is not None
    assert not any(thread.is_alive() for thread in rpc.threads)
