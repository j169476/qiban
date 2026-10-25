"""Explicit bounded role test; never starts another platform or navigates a UI.

Composition injects the existing Companion, its real model/host, and a separately
verified desktop driver. Merely constructing or importing this runner is inert.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import secrets
import time

from companion_core.errors import AssistantError

from .contracts import require


def _report():
    return {"schema_version": 1, "provider": "wechat_role_test", "status": "new",
            "handled": 0, "succeeded": 0, "uncertain": 0, "failed": 0, "cancelled": 0,
            "no_action": 0, "elapsed_seconds": 0.0, "role_stopped": True,
            "keys_exported": False, "plaintext_database_created": False}


def _check_cancelled():
    # A send adapter may consume cancellation long enough to durably mark a
    # possibly submitted action uncertain. That must not authorize the next
    # target or swallow the enclosing test deadline.
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()


async def run_role_test(receiver, model, host, driver, *, duration_seconds=180, poll_seconds=1.0, emit=None):
    """Explicit composition using the existing role core and injected real ports.

    No service restart or other-platform lifecycle operation is needed. A
    desktop provider must have been independently implemented and verified;
    this function cannot turn a name or a window handle into a trusted driver.
    """
    from companion_core.runtime import Companion

    from .gateway import PermitAuthority
    from .live_sender import LiveSenderGateway
    from .role import RoleAdapter, RoleSession

    adapter = sender = session = None
    try:
        require(driver is not None, "wechat_live_driver_missing")
        require(getattr(receiver.store, "backend", None) == "postgres" and receiver.store.db_path is None,
                "wechat_send_store_unencrypted")
        if receiver.connector is None:
            await receiver.start()
        permits = PermitAuthority(secrets.token_bytes(32))
        session = RoleSession(receiver.store, receiver.account, duration_seconds=duration_seconds)
        sender = LiveSenderGateway(receiver.store, receiver, permits, driver, session)
        adapter = RoleAdapter(receiver, sender, permits, session)
        core = Companion(receiver.store, model, host, [adapter], run_key="core-run:wechat")
        return await RoleTestRunner(core, adapter).run(poll_seconds=poll_seconds, emit=emit)
    finally:
        if adapter is not None:
            await adapter.close()
        else:
            try:
                if sender is not None:
                    await sender.close()
                elif callable(getattr(driver, "close", None)):
                    closing = driver.close()
                    if inspect.isawaitable(closing):
                        await closing
            finally:
                try:
                    close_receiver = getattr(receiver, "close", None)
                    if callable(close_receiver):
                        closing = close_receiver()
                        if inspect.isawaitable(closing):
                            await closing
                finally:
                    if session is not None:
                        session.close()


class RoleTestRunner:
    def __init__(self, core, adapter, *, clock=time.monotonic, sleep=asyncio.sleep):
        require(core.run_key == "core-run:wechat" and core.store is adapter.store
                and core.platforms.all() == [adapter], "wechat_role_scope")
        self.core, self.adapter = core, adapter
        self.clock, self.sleep = clock, sleep
        self.report = _report()

    async def run(self, *, poll_seconds=1.0, emit=None):
        # Do not start a role or spend model budget when the required live
        # desktop binding is absent. There is no fake/coordinate fallback.
        generation = None
        final_status = "complete"
        started = self.clock()
        handled_targets = set()
        last_status = None

        def publish(status):
            nonlocal last_status
            self.report["status"] = status
            self.report["elapsed_seconds"] = round(max(0.0, self.clock() - started), 3)
            # The session projects only test nonce, times, aliases and counts.
            value = {**self.report, "test": self.adapter.session.public_status()}
            signature = (status, self.report["handled"], self.report["succeeded"], self.report["uncertain"],
                         self.report["role_stopped"])
            if emit and signature != last_status:
                emit(value)
                last_status = signature
            return value

        try:
            require(type(poll_seconds) in (int, float) and math.isfinite(poll_seconds)
                    and 0.5 <= poll_seconds <= 10, "wechat_role_configuration")
            self.adapter.sender.preflight()
            current = self.core.store.state(self.core.run_key, {})
            require(current.get("status") != "active", "wechat_role_already_running")
            run = self.core.start(read_only=False)
            generation = run["generation"]
            self.report["role_stopped"] = False
            self.core.guard(generation)
            publish("ready")
            # A stalled source/model must not extend this deliberately bounded
            # test. The final send guards still check the persisted deadline.
            async with asyncio.timeout(self.adapter.session.remaining_seconds()):
                while True:
                    _check_cancelled()
                    self.core.guard(generation)
                    await self.adapter.collect_once()
                    _check_cancelled()
                    self.core.guard(generation)
                    for event in self.adapter.admitted_pending():
                        _check_cancelled()
                        self.core.guard(generation)
                        require(event.conversation in {"owner_main", "family"}, "wechat_role_scope")
                        if event.conversation in handled_targets:
                            continue
                        result = await self.core.handle(event)
                        _check_cancelled()
                        action = result.get("action") if isinstance(result, dict) else None
                        outcome = action.get("status") if isinstance(action, dict) else "no_action"
                        require(outcome in {"succeeded", "uncertain", "failed", "cancelled", "no_action"},
                                "wechat_role_result_invalid")
                        self.report[outcome] += 1
                        self.report["handled"] += 1
                        handled_targets.add(event.conversation)
                        publish("processing")
                    if len(handled_targets) == 2:
                        break
                    await self.sleep(poll_seconds)
        except TimeoutError:
            final_status = "deadline"
        except asyncio.CancelledError:
            publish("interrupted")
            raise
        except AssistantError as exc:
            if exc.code in {"duration_complete", "wechat_role_expired", "wechat_test_expired"}:
                final_status = "deadline"
            else:
                publish("blocked")
                raise
        except Exception:
            publish("failed")
            raise AssistantError("wechat_role_failed", "微信短测未完成，未确认发送不会自动重试") from None
        finally:
            try:
                if generation is not None:
                    self.core.stop("wechat_test_finished", generation=generation)
                self.report["role_stopped"] = True
            finally:
                await self.adapter.close()
                if self.report["status"] in {"blocked", "failed", "interrupted"}:
                    publish(self.report["status"])
        return publish(final_status)
