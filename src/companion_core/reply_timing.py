"""Opt-in, single-reply developer timing. No message text or model payloads."""
import inspect
import secrets
import threading
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps

from .errors import AssistantError

KEY = "developer-reply-timing"
CURRENT = ContextVar("developer_reply_timing", default=None)
PARENT = ContextVar("developer_reply_timing_parent", default=None)
TURN = ContextVar("developer_reply_model_turn", default=None)
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
        yield None
        return
    row = capture.begin(name, PARENT.get())
    token = PARENT.set(row["id"] if row is not None else PARENT.get())
    status = "completed"
    try:
        yield row
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


@contextmanager
def model_turn():
    """Measure the client side of one model turn, including failed turns."""
    from .observability import current_stage

    with span("model.request_and_wait") as row:
        if row is None:
            yield
            return
        stage = current_stage()
        row.update(purpose=stage if stage in {"recall.expand", "reply.generate", "reply.summarize"} else "other",
                   output_wait_ms=0., guard_ms=0., guard_checks=0, first_delta_ms=None)
        token = TURN.set((row, time.perf_counter()))
        try:
            yield
        finally:
            TURN.reset(token)
            for key in ("output_wait_ms", "guard_ms"):
                row[key] = round(row[key], 3)


@contextmanager
def _measure_part(row, key):
    start = time.perf_counter()
    try:
        yield
    finally:
        row[key] += (time.perf_counter() - start) * 1000
        if key == "guard_ms":
            row["guard_checks"] += 1


def model_part(key):
    turn = TURN.get()
    return _measure_part(turn[0], key) if turn else nullcontext()


def first_model_delta():
    turn = TURN.get()
    if turn and turn[0]["first_delta_ms"] is None:
        turn[0]["first_delta_ms"] = round((time.perf_counter() - turn[1]) * 1000, 3)


def wall_breakdown(rows):
    """Attribute each interval once. Parallel spans have no unique wall-time owner."""
    if not rows:
        return {}
    root = rows[0]
    start, end = root["start_ms"], root["start_ms"] + root["duration_ms"]
    edges = {start, end}
    windows = []
    for row in rows[1:]:
        left = max(start, row["start_ms"])
        right = min(end, row["start_ms"] + row["duration_ms"])
        if right > left:
            edges.update((left, right))
            windows.append((left, right, row))
    amounts = {}
    edges = sorted(edges)
    for left, right in zip(edges, edges[1:]):
        active = [row for a, b, row in windows if a <= left and right <= b]
        parents = {row["parent"] for row in active}
        leaves = [row for row in active if row["id"] not in parents]
        name = leaves[0]["stage"] if len(leaves) == 1 else ("overlap" if leaves else "reply.other")
        amounts[name] = amounts.get(name, 0.) + right - left
    return {key: round(value, 3) for key, value in amounts.items()}


def model_breakdown(rows, wall, limited):
    turns = [row for row in rows if row["stage"] == "model.request_and_wait"]
    if limited or not turns:
        return None
    window = wall.get("model.request_and_wait", 0.)
    if abs(window - sum(turn["duration_ms"] for turn in turns)) > .1:
        return None  # Parallel work cannot be assigned to a single owner.
    waiting = sum(turn["output_wait_ms"] for turn in turns)
    guards = sum(turn["guard_ms"] for turn in turns)
    setup = sum(ms for name, ms in wall.items()
                if name == "model.initialize" or name.startswith("model.initialize."))
    return {"turn_count": len(turns), "worker_output_wait_ms": round(waiting, 3),
            "local_receive_checks_ms": round(guards, 3),
            "turn_other_ms": round(max(0, window - waiting - guards), 3),
            "model_session_setup_ms": round(setup, 3),
            "outside_setup_and_turns_ms": round(sum(wall.values()) - setup - window, 3)}


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
            wall = wall_breakdown(rows)
            report = {**request, "status": status, "started_at": capture.started_at, "finished_at": time.time(),
                      "spans": rows, "limited": capture.limited,
                      "analysis": {"total_ms": total, "phases": phases, "wall_by_stage_ms": wall,
                                   "model_breakdown": model_breakdown(rows, wall, capture.limited),
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
