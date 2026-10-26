"""Receive and journal events while the single consumer is doing expensive work."""

import asyncio
from datetime import datetime, timedelta, timezone

from .errors import AssistantError
from .health import BENIGN_STOPS, fail

FATAL = {
    "login_required", "credential_invalid", "credential_refresh_required", "credential_refresh_uncertain",
    "account_mismatch", "account_changed", "keyring_unavailable", "critical_state_invalid", "supervisor_lost",
    "host_unavailable", "host_timeout", "host_protocol_error", "stale_generation", *BENIGN_STOPS,
}


class InboxWorker:
    def __init__(self, engine, generation):
        self.engine, self.generation = engine, generation
        self.wake = asyncio.Event()
        self.values = {"generation": generation, "phase": "starting", "failures": 0,
                       "last_error": None, "last_success_at": None, "received": 0, "next_poll_at": None}

    def pulse(self, **values):
        self.values.update(values)
        self.engine.store.set_state(f"inbox:{self.generation}", {
            **self.values, "at": datetime.now(timezone.utc).isoformat(),
        })

    async def poll(self):
        self.engine.run(self.generation)
        settings = self.engine.store.read("companion")["data"]
        interval = settings.get("inbox_poll_seconds", 60)
        self.pulse(phase="receiving", wait_reason=None)
        try:
            result = await asyncio.wait_for(
                self.engine.inbox(archive=False, report=True),
                timeout=settings.get("inbox_timeout_seconds", 180),
            )
        except TimeoutError:
            raise AssistantError("inbox_timeout", "本轮收件超时") from None
        self.engine.run(self.generation)
        self.pulse(phase="waiting", failures=0, last_error=None,
                   received=self.values["received"] + result["new_count"],
                   last_success_at=datetime.now(timezone.utc).isoformat())
        if result["new_count"]:
            self.wake.set()
        return interval

    async def run(self):
        try:
            while True:
                self.engine.run(self.generation)
                try:
                    seconds = await self.poll()
                except AssistantError as exc:
                    failures = self.values["failures"] + 1
                    self.pulse(phase="backoff", failures=failures, last_error=exc.code)
                    if exc.code in FATAL or failures >= 3:
                        fail(self.engine, exc.code if exc.code in FATAL else "inbox_repeated_failure", self.generation)
                        return
                    base = self.engine.store.read("companion")["data"].get("inbox_poll_seconds", 60)
                    seconds = min(900, base * 2 ** failures)
                self.pulse(next_poll_at=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat())
                await asyncio.sleep(seconds)
        except AssistantError as exc:
            fail(self.engine, exc.code, self.generation)
        except Exception:
            fail(self.engine, "inbox_repeated_failure", self.generation)
        finally:
            self.pulse(phase="stopped", next_poll_at=None)
