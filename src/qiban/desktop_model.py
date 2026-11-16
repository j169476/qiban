"""Application-account model adapter; no global Codex login or paid fallback."""
import asyncio
from dataclasses import dataclass, field

from companion_core.errors import AssistantError
from companion_core.observability import current_stage, measure

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

    async def generate(self, instructions, context, output_schema, images=(), *, native=None,
                       store, policy, guard, phase=lambda value: None):
        if native is not None and native not in self.supported_native:
            raise AssistantError('model_capability_unavailable', '此订阅执行入口不支持该原生工具。')
        async with self.capacity:
            await asyncio.to_thread(guard)
            access = await asyncio.to_thread(self.broker.model_access)
            model, effort = await asyncio.to_thread(self.catalog.resolve, self.broker.store, access)

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
                        transport=self.web_transport)
                from companion_codex.app_server import generate

                return await generate(instructions, context, output_schema, images,
                    native=native,
                    store=store, policy=policy, guard=check, phase=phase, observation=observation,
                    model=model, reasoning_effort=effort,
                    plan_access_token=access.access_token, executable=self.executable)
