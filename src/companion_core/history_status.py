"""Trusted collection status; missing history is distinct from context truncation."""
from .errors import AssistantError

REASONS = {
    "checkpoint_unavailable": "无法取回用于衔接的历史记录；具体原因未确认。",
    "history_unavailable": "平台未能提供部分历史记录；具体原因未确认。",
    "history_query_failed": "历史读取请求未成功，已有消息之外的记录未能核实。",
    "history_read_stalled": "历史读取未能继续向前推进，较早记录未能核实。",
    "history_page_limit": "已达到本次历史读取页数上限，剩余历史未读取。",
    "source_deleted": "已确认部分历史记录被删除或撤回。",
    "retention_expired": "已确认部分记录超过平台可提供的历史保存期限。",
    "unknown": "聊天记录不完整，原因未确认。",
}


def validate_history_status(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"status", "reasons"}
            or value["status"] not in {"complete", "incomplete"}
            or not isinstance(value["reasons"], list) or len(value["reasons"]) > len(REASONS)
            or any(not isinstance(reason, str) or reason not in REASONS for reason in value["reasons"])
            or (value["status"] == "complete") != (not value["reasons"])):
        raise AssistantError("history_status_invalid", "历史完整性记录无效。")
    return {"status": value["status"], "reasons": list(dict.fromkeys(value["reasons"]))}


def history_notice(value):
    status = validate_history_status(value)
    if not status or status["status"] == "complete":
        return None
    return {
        "complete": False,
        "notice": "聊天记录不完整。",
        "reasons": [{"code": code, "text": REASONS[code]} for code in status["reasons"]],
        "guidance": "只根据已提供的内容继续理解和回复；不要补造缺失对话，不要将缺失当作从未发生，"
                    "不得声称读完全部聊天记录。原因未确认时不要推断为删除、撤回或对方故意隐瞒。",
    }
