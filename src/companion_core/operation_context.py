"""Cooperative deadlines and safe stage diagnostics, without request contents."""
import json
import os
import re
import sys
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .errors import AssistantError

_deadline = ContextVar("operation_deadline", default=None)
_trace = ContextVar("operation_trace", default=None)


@contextmanager
def bounded(seconds, code="control_timeout"):
    previous = _deadline.get()
    limit = (time.monotonic() + seconds, code)
    if previous and previous[0] < limit[0]:
        limit = previous
    token = _deadline.set(limit)
    try:
        checkpoint()
        yield
    finally:
        _deadline.reset(token)


def remaining(cap):
    limit = _deadline.get()
    value = cap if limit is None else min(cap, limit[0] - time.monotonic())
    if value <= 0:
        raise AssistantError(limit[1], "等待已超过当前操作预算，请查看阶段诊断；先查询状态再决定是否重试")
    return value


def checkpoint():
    remaining(1)


@contextmanager
def detached_trace():
    """Background tasks must not keep writing their parent's startup trace."""
    token = _trace.set(None)
    try:
        yield
    finally:
        _trace.reset(token)


@contextmanager
def locked(lock, timeout=10):
    if not lock.acquire(timeout=remaining(timeout)):
        raise AssistantError("control_busy", "控制入口正在处理其他操作，请稍后查询状态")
    try:
        checkpoint()
        yield
    finally:
        lock.release()


def safe_failure(exc):
    code = getattr(exc, "code", "internal_error")
    if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", code):
        code = "internal_error"
    frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
              for frame in traceback.extract_tb(exc.__traceback__)
              if any(part.startswith(("companion_", "bili_assistant", "qiban"))
                     for part in Path(frame.filename).parts)]
    return {"code": code, "exception": type(exc).__name__, "frames": frames[-8:]}


class Trace:
    def __init__(self, component, *, emit=True, path=None):
        self.component, self.emit, self.path = component, emit, path
        self.started = time.monotonic()
        self.steps = []

    def snapshot(self):
        return {"component": self.component, "pid": os.getpid(), "elapsed_ms": round((time.monotonic() - self.started) * 1000),
                "stages": [dict(step) for step in self.steps]}

    def write(self, value):
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
                temporary.write_text(json.dumps(self.snapshot()), encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                pass  # Diagnostics must not change authentication or recovery semantics.
        if self.emit:
            # Progress goes to stderr; stdout remains one machine-readable result.
            try:
                print(json.dumps({"qiban_diagnostic": self.component, **value}, ensure_ascii=False),
                      file=sys.stderr, flush=True)
            except OSError:
                pass  # A closed progress reader must not abort the operation.

    @contextmanager
    def activate(self):
        token = _trace.set(self)
        try:
            yield self
        except AssistantError as exc:
            exc.diagnostics = self.snapshot()
            raise
        finally:
            _trace.reset(token)


@contextmanager
def stage(name):
    trace = _trace.get()
    started = time.monotonic()
    record = {"stage": name, "status": "running"}
    if trace:
        if len(trace.steps) >= 64:
            trace.steps.pop(0)
        trace.steps.append(record)
        trace.write(record)
    try:
        checkpoint()
        yield
        record["status"] = "completed"
    except BaseException as exc:
        record.update(status="failed", **safe_failure(exc))
        raise
    finally:
        record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        if trace:
            trace.write(record)
