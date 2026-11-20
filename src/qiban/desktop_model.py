"""Application-account model adapter; no global Codex login or paid fallback."""
import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from companion_core.errors import AssistantError
from companion_core.observability import current_stage, measure
from companion_core.reply_timing import span

from .desktop_model_catalog import ModelCatalog


@dataclass(frozen=True)
class ModelAccess:
    account_id: str
    access_token: str = field(repr=False)


class DesktopModel:
    # Protocol support, not a claim that every model/account has web entitlement.
    supported_native = frozenset({'web'})

    def __init__(self, broker, *, executable, web_transport=None):
        self.broker, self.executable = broker, executable
        self.catalog = ModelCatalog(broker)
        # Offline test seam for the direct web route; None uses the official HTTPS client.
        self.web_transport = web_transport
        # One owned model worker at a time; queued work reads the account only
        # when it obtains admission, so it cannot retain an old token.
        self.capacity = asyncio.Semaphore(1)

    async def _refresh_account(self):
        # A dispatched rotation must settle before another turn acquires capacity.
        job = asyncio.create_task(asyncio.to_thread(self.broker.refresh_for_platform))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            await job
            raise

    async def maintain_account(self, wanted):
        if not await asyncio.to_thread(self.broker.platform_refresh_due):
            return
        async with self.capacity:
            if await asyncio.to_thread(wanted):
                await self._refresh_account()

    async def generate(self, instructions, context, output_schema, images=(), *, native=None,
                       store, policy, guard, phase=lambda value: None):
        if native is not None and native not in self.supported_native:
            raise AssistantError('model_capability_unavailable', '此订阅执行入口不支持该原生工具。')
        async with AsyncExitStack() as admission:
            with span("model.capacity_wait"):
                await admission.enter_async_context(self.capacity)
            await asyncio.to_thread(guard)
            try:
                with span("model.account_access"):
                    access = await asyncio.to_thread(self.broker.model_access)
            except AssistantError as error:
                if error.code != 'model_auth':
                    raise
                # Resume from sleep can put the first message ahead of maintenance.
                with span("model.account_refresh"):
                    if not await self._refresh_account():
                        raise
                await asyncio.to_thread(guard)
                with span("model.account_access"):
                    access = await asyncio.to_thread(self.broker.model_access)
            with span("model.catalog_resolve"):
                model, effort, tier = await asyncio.to_thread(self.catalog.resolve, self.broker.store, access)

            def check():
                guard()
                self.broker.check_model_access(access)

            with measure(store, current_stage(), kind='model') as observation:
                if native == 'web':
                    # Approved exception: web-research steps use the existing SIWC
                    # authorization directly; every other step stays on app-server.
                    from companion_codex.plan_web import generate as generate_web

                    return await generate_web(instructions, context, output_schema, images,
                        policy=policy, guard=check, phase=phase, observation=observation,
                        access=access, model=model, reasoning_effort=effort,
                        transport=self.web_transport, service_tier=tier)
                from companion_codex.app_server import generate

                return await generate(instructions, context, output_schema, images,
                    native=native,
                    store=store, policy=policy, guard=check, phase=phase, observation=observation,
                    model=model, reasoning_effort=effort,
                    plan_access_token=access.access_token, executable=self.executable, service_tier=tier)
