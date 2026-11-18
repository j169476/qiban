"""Opt-in, single-reply developer timing. No message text or model payloads."""
import inspect
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .errors import AssistantError

KEY = "developer-reply-timing"
CURRENT = ContextVar("developer_reply_timing", default=None)
PARENT = ContextVar("developer_reply_timing_parent", default=None)
PLATFORMS = {"qq", "wechat", "bilibili", "local"}


def control(store, action, platform="qq", *, now=None):
    """Local backend only; one pending/capturing request per encrypted store."""
    now = time.time() if now is None else now
    if action not in {"arm", "report", "cancel"} or platform not in PLATFORMS:
        raise AssistantError("timing_request", "计时操作或平台无效")
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        value = store.state(KEY, {}, db)
        if action == "report":
            if value.get("status") in {"armed", "capturing"} and now >= value["expires_at"]:
                return {**value, "status": "expired"}
            return value or {"status": "disabled"}
        if action == "cancel":
            value = {"status": "cancelled"}
        else:
            if value.get("status") in {"armed", "capturing"} and now < value["expires_at"]:
                raise AssistantError("timing_busy", "已有待采集的回复，请先取消或等待完成")
            value = {"id": secrets.token_hex(16), "status": "armed", "platform": platform,
                     "armed_at": now, "expires_at": now + 600}
        store.set_state(KEY, value, db)
        return value


def _claim(store, platform):
    # The disabled path reads one small setting and creates no detailed timers.
    value = store.state(KEY, {})
    if value.get("status") != "armed" or value.get("platform") != platform:
        return None
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        value = store.state(KEY, {}, db)
        if (value.get("status") != "armed" or value.get("platform") != platform
                or time.time() >= value["expires_at"]):
            return None
        value = {**value, "status": "capturing"}
        store.set_state(KEY, value, db)
        return value


class Capture:
    def __init__(self):
        self.start = time.perf_counter()
        self.started_at = time.time()
        self.rows, self.lock, self.phase = [], threading.RLock(), None
        self.limited = self.closed = False

    def begin(self, name, parent):
        with self.lock:
            if self.closed or len(self.rows) >= 256:
                self.limited = True
                return None
            row = {"id": len(self.rows), "parent": parent, "stage": name,
                   "start_ms": (time.perf_counter() - self.start) * 1000}
            self.rows.append(row)
            return row

    def end(self, row, status="completed"):
        with self.lock:
            if row is not None and "end_ms" not in row:
                row.update(end_ms=(time.perf_counter() - self.start) * 1000, status=status)

    def report(self):
        with self.lock:
            self.closed = True
            for row in self.rows:
                if "end_ms" not in row:
                    self.end(row, "interrupted")
            result = []
            for row in self.rows:
                # Union child intervals: concurrent children must not be added twice.
                intervals = sorted((max(row["start_ms"], c["start_ms"]), min(row["end_ms"], c["end_ms"]))
                                   for c in self.rows if c["parent"] == row["id"])
                covered, edge = 0, row["start_ms"]
                for start, end in intervals:
                    covered += max(0, end - max(edge, start))
                    edge = max(edge, end)
                duration = max(0, row["end_ms"] - row["start_ms"])
                result.append({**{k: v for k, v in row.items() if k != "end_ms"},
                               "start_ms": round(row["start_ms"], 3), "duration_ms": round(duration, 3),
                               "self_ms": round(max(0, duration - covered), 3)})
            return result


@contextmanager
def span(name):
    capture = CURRENT.get()
    if capture is None:
        yield
        return
    row = capture.begin(name, PARENT.get())
    token = PARENT.set(row["id"] if row is not None else PARENT.get())
    status = "completed"
    try:
        yield
    except BaseException:
        status = "failed_or_cancelled"
        raise
    finally:
        capture.end(row, status)
        PARENT.reset(token)


def phase(name):
    capture = CURRENT.get()
    if capture is not None:
        capture.end(capture.phase)
        capture.phase = capture.begin(name, 0)
        PARENT.set(capture.phase["id"] if capture.phase else 0)


def timed(name):
    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def wrapped(*args, **kwargs):
                with span(name):
                    return await fn(*args, **kwargs)
        else:
            @wraps(fn)
            def wrapped(*args, **kwargs):
                with span(name):
                    return fn(*args, **kwargs)
        return wrapped
    return decorate


@contextmanager
def capture_reply(store, event):
    request = None
    try:
        request = _claim(store, event.platform)
    except Exception:
        pass  # Developer diagnostics cannot prevent a reply.
    if request is None:
        yield
        return
    capture = Capture()
    token = CURRENT.set(capture)
    parent = PARENT.set(None)
    status = "completed"
    try:
        with span("reply.total"):
            try:
                yield
            except BaseException:
                status = "failed_or_cancelled"
                raise
            finally:
                capture.end(capture.phase, status)
    finally:
        CURRENT.reset(token)
        PARENT.reset(parent)
        try:
            rows = capture.report()
            total = rows[0]["duration_ms"] if rows else 0
            phases = [{"stage": row["stage"], "duration_ms": row["duration_ms"],
                       "percent": round(row["duration_ms"] / total * 100, 2) if total else 0}
                      for row in rows if row["parent"] == 0]
            report = {**request, "status": status, "started_at": capture.started_at, "finished_at": time.time(),
                      "spans": rows, "limited": capture.limited,
                      "analysis": {"total_ms": total, "phases": phases,
                                   "largest_phase": max(phases, key=lambda row: row["duration_ms"], default=None),
                                   "unattributed_ms": rows[0]["self_ms"] if rows else 0},
                      "scope": "reply_handler_through_delivery; excludes platform polling before handler",
                      "units": "milliseconds", "nested": True}
            with store.lock, store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                current = store.state(KEY, {}, db)
                if current.get("id") == request["id"] and current.get("status") == "capturing":
                    store.set_state(KEY, report, db)
        except Exception:
            pass  # Never turn diagnostic persistence failure into another send.
