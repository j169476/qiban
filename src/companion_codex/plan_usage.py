"""Read-only, bounded Codex plan snapshots. No credentials or billing mutations."""
import math
import time
from copy import deepcopy
from threading import Lock

from companion_core.errors import AssistantError


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def normalize(payload):
    if not isinstance(payload, dict):
        return []
    buckets = payload.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = payload.get("rateLimits")
        buckets = {"codex": legacy} if isinstance(legacy, dict) else {}
    result = []
    for key, bucket in list(buckets.items())[:20]:
        if not isinstance(bucket, dict):
            continue
        windows = []
        for name in ("primary", "secondary"):
            value = bucket.get(name)
            if not isinstance(value, dict):
                continue
            used = number(value.get("usedPercent"))
            windows.append({"name": name, "used_percent": used,
                "remaining_percent": max(0, min(100, 100 - used)) if used is not None else None,
                "window_minutes": number(value.get("windowDurationMins")),
                "resets_at": number(value.get("resetsAt"))})
        result.append({"id": str(key)[:100], "name": str(bucket.get("limitName") or key)[:100],
                       "plan": bucket.get("planType") if isinstance(bucket.get("planType"), str) else None,
                       "windows": windows})
    return result


class PlanUsage:
    def __init__(self, client, *, clock=time.time):
        self.client, self.clock = client, clock
        self.lock = Lock()
        self.value = {"status": "unavailable", "updated_at": None, "attempted_at": None, "buckets": [],
                      "scope": "account", "source": "account/rateLimits/read"}

    def snapshot(self):
        with self.lock:
            value = deepcopy(self.value)
        value["stale"] = value["updated_at"] is None or self.clock() - value["updated_at"] > 120
        return value

    def refresh(self):
        now = self.clock()
        with self.lock:
            if self.value["attempted_at"] is not None and now - self.value["attempted_at"] < 60:
                return
            self.value["attempted_at"] = now
        try:
            buckets = normalize(self.client.request("account/rateLimits/read", {}))
            with self.lock:
                self.value.update(status="available" if buckets else "unavailable", buckets=buckets,
                                  updated_at=now, error=None)
        except (AssistantError, OSError, ValueError, TypeError):
            with self.lock:
                self.value.update(status="unavailable", error="plan_usage_unavailable")

    def close(self):
        self.client.close()
