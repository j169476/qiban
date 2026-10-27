"""Local bounded metadata ledger; never retains prompts or raw model events."""
import asyncio
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from .errors import AssistantError

TOKENS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
_stage = ContextVar("companion_observation_stage", default="unclassified")


def current_stage():
    return _stage.get()


@contextmanager
def stage(value):
    """Label a real model call without creating a second timing observation."""
    token = _stage.set(value)
    try:
        yield
    finally:
        _stage.reset(token)


def label(value, default="unknown"):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,100}", value) else default


class Observation:
    def __init__(self, store, stage, kind, generation=None):
        self.store, self.stage, self.kind = store, label(stage), label(kind)
        self.generation = generation
        self.started = datetime.now(timezone.utc)
        self.id = f"{self.started.isoformat()}-{uuid.uuid4().hex}"
        self.monotonic = time.monotonic()
        self.usage = {key: None for key in TOKENS}
        self.usage_events = 0
        self.enabled = callable(getattr(store, "db", None))

    def write(self, sql, args):
        if not self.enabled:
            return
        try:
            with self.store.db() as db:
                db.execute(sql, args)
        except (sqlite3.Error, OSError, AssistantError):
            # Diagnostics must not turn an already-sent action into a retry.
            self.store._observation_write_failures = getattr(self.store, "_observation_write_failures", 0) + 1

    def start(self):
        self.write("INSERT INTO core_observations(id,kind,stage,started,status) VALUES(?,?,?,?,?)",
                   (self.id, self.kind, self.stage, self.started.isoformat(), "running"))
        # Retain 90 days; the public report supports at most 30 days. The timestamp
        # prefix uses the primary-key index, without adding backup-incompatible indexes.
        cutoff = (self.started - timedelta(days=90)).isoformat()
        self.write("DELETE FROM core_observations WHERE id < ?", (cutoff,))

    def tokens(self, values):
        for key in TOKENS:
            value = values.get(key)
            self.usage[key] = value if type(value) is int and 0 <= value <= 10**12 else None
        count = values.get("usage_events", 0)
        self.usage_events = count if type(count) is int and 0 <= count <= 10000 else 0

    def finish(self, error):
        status = "success" if error is None else "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        code = label(error.code) if isinstance(error, AssistantError) else "cancelled" if status == "cancelled" else None
        if error is not None and code is None:
            code = "unexpected_error"
        if error is not None and not isinstance(error, asyncio.CancelledError):
            from .diagnostics import record_exception

            if isinstance(error, AssistantError) and not hasattr(error, "runtime_stage"):
                error.runtime_stage = self.stage
            record_exception(self.store, error, stage=self.stage, generation=self.generation, observation_id=self.id)
        elapsed = max(0, round((time.monotonic() - self.monotonic) * 1000))
        from .reply_trace import point

        point(self.stage, observation_id=self.id, kind=self.kind, status=status, duration_ms=elapsed,
              error_code=code, **self.usage)
        self.write("""UPDATE core_observations SET finished=?,status=?,error_code=?,duration_ms=?,
                   input_tokens=?,cached_input_tokens=?,output_tokens=?,reasoning_output_tokens=?,usage_events=?
                   WHERE id=?""", (datetime.now(timezone.utc).isoformat(), status, code, elapsed,
                                  *(self.usage[key] for key in TOKENS), self.usage_events, self.id))


@contextmanager
def measure(store, stage, kind="stage", *, generation=None):
    observation = Observation(store, stage, kind, generation)
    token = _stage.set(observation.stage)
    error = None
    try:
        observation.start()
        yield observation
    except BaseException as exc:
        error = exc
        raise
    finally:
        try:
            observation.finish(error)
        finally:
            _stage.reset(token)


def report(store, days=1, limit=50):
    if type(days) is not int or not 1 <= days <= 30 or type(limit) is not int or not 1 <= limit <= 200:
        raise AssistantError("invalid_arguments", "观测窗口为 1–30 天，明细上限为 1–200 条")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    groups, latest = {}, []
    with store.db() as db:
        for row in db.execute("SELECT * FROM core_observations WHERE id >= ? ORDER BY id DESC", (since,)):
            row = dict(row)
            if len(latest) < limit:
                latest.append(row)
            key = (row["kind"], row["stage"])
            if key not in groups:
                groups[key] = {"kind": key[0], "stage": key[1], "calls": 0, "statuses": {},
                               "duration_ms": 0, "duration_known_calls": 0,
                               "known_tokens": {key: 0 for key in TOKENS},
                               "token_known_calls": {key: 0 for key in TOKENS}, "usage_events": 0}
            group = groups[key]
            group["calls"] += 1
            status = row["status"]
            group["statuses"][status] = group["statuses"].get(status, 0) + 1
            if row["duration_ms"] is not None:
                group["duration_ms"] += row["duration_ms"]
                group["duration_known_calls"] += 1
            group["usage_events"] += row["usage_events"]
            for key in TOKENS:
                if row[key] is not None:
                    group["known_tokens"][key] += row[key]
                    group["token_known_calls"][key] += 1
        for group in groups.values():
            # No observed usage is unknown, not a claim that a call was free.
            for key in TOKENS:
                if not group["token_known_calls"][key]:
                    group["known_tokens"][key] = None
    from .diagnostics import KEY

    diagnostics = {item.get("observation_id"): item for item in store.state(KEY, [])}
    for item in latest:
        if item["id"] in diagnostics:
            item["diagnostic"] = diagnostics[item["id"]]
    return {"since": since, "days": days, "retention_days": 90, "groups": list(groups.values()),
            "recent": latest, "write_failures_this_process": getattr(store, "_observation_write_failures", 0),
            "money_cost": None, "running_may_be_interrupted": True,
            "notes": ["token 数仅来自 Codex turn.completed usage；缺失值未知。",
                      "cached_input_tokens 包含于 input_tokens，reasoning_output_tokens 包含于 output_tokens，不重复相加。",
                      "阶段耗时含模型时间，阶段与模型耗时不能相加；running 仅表示未记录结束，不证明进程仍活跃。",
                      "窗口为最近滚动天数，不等同于每日额度账本；不估算订阅金额。"]}
