"""Bounded reply metadata, correlated across async stages. Never stores prompt text."""
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from .errors import AssistantError

CURRENT = ContextVar("companion_reply_trace", default=None)
PREFIX = "reply-trace:"


def point(stage, **metadata):
    trace = CURRENT.get()
    if trace is not None and len(trace["steps"]) < 100:
        trace["steps"].append({"stage": stage, **metadata})


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


def sources(pins):
    trace = CURRENT.get()
    if trace is not None:
        items = sorted(dict(pins).items())
        trace["source_count"] = len(items)
        trace["sources"] = [{"resource": key, "version": version} for key, version in items[:100]]
        trace["sources_limited"] = len(items) > 100


def listing(store, limit=30):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise AssistantError("invalid_query", "回复追踪数量须为 1 至 100")
    with store.db() as db:
        keys = db.execute("SELECT key FROM state WHERE key LIKE ? ORDER BY key DESC LIMIT ?",
                          (PREFIX + "%", limit)).fetchall()
    return [{key: value for key, value in store.state(row[0], {}).items() if key not in {"sources", "steps"}}
            for row in keys]


def detail(store, identifier, access, request):
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 100:
        raise AssistantError("invalid_query", "回复追踪标识无效")
    trace = store.state(PREFIX + identifier)
    if not trace:
        raise AssistantError("not_found", "回复追踪不存在或已过期")
    visible = []
    for source in trace.get("sources", []):
        if access.can_read(request, source["resource"]):
            policy = access.repository.policy(source["resource"])
            visible.append({**source, "version_current": bool(policy and policy.version == source["version"])})
    return {**trace, "sources": visible, "hidden_sources": len(trace.get("sources", [])) - len(visible),
            "meaning": "程序处理记录；不保存提示词、原文或模型内部推理。来源按当前权限再次过滤。"}
