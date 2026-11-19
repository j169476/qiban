"""Bounded reply metadata, correlated across async stages. Never stores prompt text."""
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from .errors import AssistantError

CURRENT = ContextVar("companion_reply_trace", default=None)
PREFIX = "reply-trace:"
OUTCOMES = {"succeeded", "failed", "cancelled", "uncertain", "duplicate", "deferred", "no_action"}
DISPLAYABLE_ACTION_ERRORS = {
    "wechat_driver_prepare_failed", "wechat_prepare_timeout", "wechat_prepare_access_denied",
    "wechat_prepare_resource_missing", "wechat_binding_changed", "wechat_send_target_changed",
    "wechat_send_target_unverified", "wechat_send_text_invalid", "wechat_native_closed",
    "wechat_stale_connection", "wechat_send_unconfirmed", "wechat_task_cancelled",
    "wechat_prepare_version_unsupported", "wechat_prepare_client_unavailable", "wechat_prepare_window_not_minimized",
    "wechat_receipt_unavailable", "wechat_receipt_disconnected",
    "wechat_transport_failed", "wechat_provider_rejected", "qq_send_unconfirmed",
    "core_before_send_cancelled", "invalid_connector_result", "transport_or_unknown",
}


def point(stage, **metadata):
    trace = CURRENT.get()
    if trace is None:
        return
    if stage == "outcome" and metadata.get("status") in OUTCOMES:
        trace["outcome"] = metadata["status"]
        if metadata.get("error_code"):
            trace["outcome_error"] = metadata["error_code"]
    if len(trace["steps"]) < 100:
        trace["steps"].append({"stage": stage, **metadata})


def _with_outcome(trace):
    """Older records stored the action outcome only in the final diagnostic step."""
    if "outcome" not in trace:
        for step in reversed(trace.get("steps", [])):
            if step.get("stage") == "outcome" and step.get("status") in OUTCOMES:
                return {**trace, "outcome": step["status"]}
    return trace


def record_outcome(result):
    action = result.get("action") or {}
    state = action.get("status")
    if not state:
        state = "deferred" if result.get("deferred") else "duplicate" if result.get("duplicate") else "no_action"
    error = action.get("error")
    if error:
        code = error if isinstance(error, str) and error in DISPLAYABLE_ACTION_ERRORS else "other_action_error"
        point("outcome", status=state, error_code=code)
    else:
        point("outcome", status=state)


@contextmanager
def reply(store, event, run):
    started = datetime.now(timezone.utc)
    value = {"id": started.isoformat() + ":" + uuid.uuid4().hex,
             "event_id": event.id, "platform": event.platform, "generation": run["generation"],
             "started": started.isoformat(), "status": "running", "steps": [], "sources": []}
    marker = CURRENT.set(value)
    clock = time.monotonic()

    def persist():
        try:
            with store.db() as db:
                store.set_state(PREFIX + value["id"], value, db)
                # Bounded retention by time and count, including abandoned running traces.
                cutoff = PREFIX + (started - timedelta(days=30)).isoformat()
                db.execute("DELETE FROM state WHERE key LIKE ? AND key < ?", (PREFIX + "%", cutoff))
                rows = db.execute("SELECT key FROM state WHERE key LIKE ? ORDER BY key DESC LIMIT 1000 OFFSET 500",
                                  (PREFIX + "%",)).fetchall()
                for row in rows:
                    db.execute("DELETE FROM state WHERE key=?", (row[0],))
        except Exception:
            # Diagnostic failure must never retry a confirmed/uncertain platform action.
            store._reply_trace_failures = getattr(store, "_reply_trace_failures", 0) + 1

    persist()
    try:
        yield value
        value["status"] = "completed"
    except BaseException as exc:
        from .observability import label

        value["status"] = "failed" if isinstance(exc, Exception) else "cancelled"
        value["error"] = label(exc.code) if isinstance(exc, AssistantError) else "interrupted_or_internal_error"
        raise
    finally:
        value["duration_ms"] = round((time.monotonic() - clock) * 1000)
        persist()
        CURRENT.reset(marker)


def sources(pins, *, roots=None):
    trace = CURRENT.get()
    if trace is not None:
        items = sorted(dict(pins).items())
        trace["source_count"] = len(items)
        trace["sources"] = [{"resource": key, "version": version} for key, version in items[:100]]
        trace["sources_limited"] = len(items) > 100
        trace['retention_roots'] = list(roots) if roots is not None else [key for key, _ in items]


def listing(store, limit=30):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise AssistantError("invalid_query", "回复追踪数量须为 1 至 100")
    with store.db() as db:
        keys = db.execute("SELECT key FROM state WHERE key LIKE ? ORDER BY key DESC LIMIT ?",
                          (PREFIX + "%", limit)).fetchall()
    return [{key: value for key, value in _with_outcome(store.state(row[0], {})).items() if key not in {"sources", "steps", "retention_roots"}}
            for row in keys]


def detail(store, identifier, access, request):
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 100:
        raise AssistantError("invalid_query", "回复追踪标识无效")
    trace = store.state(PREFIX + identifier)
    if not trace:
        raise AssistantError("not_found", "回复追踪不存在或已过期")
    trace = _with_outcome(trace)
    visible = []
    for source in trace.get("sources", []):
        if access.can_read(request, source["resource"]):
            policy = access.repository.policy(source["resource"])
            visible.append({**source, "version_current": bool(policy and policy.version == source["version"])})
    steps = []
    for step in trace.get("steps", []):
        if "source_choices" in step:
            choices = [row for row in step["source_choices"] if access.can_read(request, row["resource"])]
            step = {**step, "source_choices": choices}
        steps.append(step)
    return {**{key: value for key, value in trace.items() if key != 'retention_roots'}, "steps": steps,
            "sources": visible, "hidden_sources": len(trace.get("sources", [])) - len(visible),
            "meaning": "程序处理记录；不保存提示词、原文或模型内部推理。来源按当前权限再次过滤。"}
