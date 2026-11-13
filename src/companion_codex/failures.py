"""Public failure messages from structured app-server fields, never error prose."""

from companion_core.errors import AssistantError

MESSAGES = {
    "model_usage_limit": "当前模型使用额度达到限制，请查看 ChatGPT 用量设置；限制也可能仅针对本应用。",
    "model_rate_limit": "模型请求受到限流，请稍后再试并检查用量设置。",
    "model_auth": "模型授权未获接受，请检查当前账号和授权状态。",
    "model_permission": "模型请求受权限或服务政策限制，请检查当前账号与服务配置。",
    "model_unavailable": "所选模型当前不可用，请检查账号权限和模型配置。",
    "model_temporary": "模型服务暂时不可用，请稍后再试。",
    "model_input_size": "本轮内容超出模型上下文上限，请缩小输入范围。",
    "model_request_invalid": "模型请求不受支持或参数无效，请检查请求配置。",
    "model_budget": "本轮任务达到预算限制，已停止处理。",
    "model_interrupted": "本轮模型处理已中断，未取得完整结果。",
    "model_failed": "模型调用未完成，未取得完整结果。",
}
KINDS = {
    "usageLimitExceeded": "model_usage_limit",
    "rateLimitExceeded": "model_rate_limit",
    "unauthorized": "model_auth",
    "modelNotFound": "model_unavailable",
    "serverOverloaded": "model_temporary",
    "internalServerError": "model_temporary",
    "flexUnavailable": "model_temporary",
    "contextWindowExceeded": "model_input_size",
    "sessionBudgetExceeded": "model_budget",
    "badRequest": "model_request_invalid",
    "cyberPolicy": "model_permission",
    "misalignmentPolicyViolation": "model_permission",
}
HTTP_KINDS = frozenset(
    {
        "httpConnectionFailed",
        "responseStreamConnectionFailed",
        "responseStreamDisconnected",
        "responseTooManyFailedAttempts",
    }
)


def turn_failure(status, error):
    """Classify without retrying, changing accounts/routes, or erasing credentials.

    app-server does not promise the Responses error.code survives its translation.
    Unknown variants and additionalDetails remain generic; never substring-match
    messages, which can contain request content, credentials, or misleading text.
    """
    code = "model_interrupted" if status == "interrupted" else "model_failed"
    diagnostic = {"completion_status": status if status in ("failed", "interrupted") else "unknown"}
    if status != "interrupted" and isinstance(error, dict):
        info = error.get("codexErrorInfo")
        if isinstance(info, str) and info in KINDS:
            diagnostic["codex_error"] = info
            code = KINDS[info]
        elif isinstance(info, dict) and len(info) == 1:
            kind, detail = next(iter(info.items()))
            if kind in HTTP_KINDS and isinstance(detail, dict):
                diagnostic["codex_error"] = kind
                http = detail.get("httpStatusCode")
                if type(http) is int and 100 <= http <= 599:
                    diagnostic["http_status"] = http
                    code = {
                        400: "model_request_invalid",
                        401: "model_auth",
                        403: "model_permission",
                        429: "model_rate_limit",
                    }.get(http, code)
                    if 500 <= http <= 599:
                        code = "model_temporary"
    failure = AssistantError(code, MESSAGES[code])
    failure.diagnostics = diagnostic
    return failure
