"""Platform-neutral bounded stages, shared by compatibility and native core runs."""
import inspect
from dataclasses import dataclass, field
from enum import Enum

from filelock import FileLock, Timeout

from .contracts import stable_key
from .errors import AssistantError
from .execution import action_lock
from .observability import measure


class Flow(str, Enum):
    CONTINUE = "continue"
    FINALIZE = "finalize"
    STOP = "stop"


@dataclass(frozen=True)
class Stage:
    name: str
    callback: object
    priority: int
    lane: str = "work"
    requires_model: bool = False


@dataclass
class Workflow:
    platform: str
    account: str
    stages: tuple
    data: dict = field(default_factory=dict)

    @property
    def key(self):
        return stable_key("workflow", self.platform, self.account)


async def run_cycle(store, workflows, *, guard, remaining_model_calls, phase=None, checkpoint=None):
    """Run each platform's registered stages fairly; no platform imports live here.

    FINALIZE skips the remaining work stages but still permits reflection and
    maintenance. STOP ends only this workflow, allowing other platforms to collect
    events. Every stage has a fresh lifecycle guard and model-budget check.
    """
    workflows = sorted(workflows, key=lambda workflow: (workflow.platform, workflow.account))
    if len({workflow.key for workflow in workflows}) != len(workflows):
        raise AssistantError("duplicate_workflow", "同一平台账号只能注册一个工作流")
    for workflow in workflows:
        if len({stage.name for stage in workflow.stages}) != len(workflow.stages):
            raise AssistantError("duplicate_stage", "工作流阶段名称必须唯一")
        if any(stage.lane not in {"prepare", "work", "finalize"} for stage in workflow.stages):
            raise AssistantError("invalid_stage", "工作流阶段类型无效")
    async with action_lock(store, "workflow-cycle"):
        lock = FileLock(str(store.runtime / "cycle.lock"), timeout=0)
        try:
            lock.acquire()
        except Timeout:
            raise AssistantError("cycle_busy", "另一执行者正在处理本地角色周期") from None
        try:
            run = guard()
            cursor_key = stable_key("workflow-turn", run["owner"], *[workflow.key for workflow in workflows])
            offset = store.state(cursor_key, 0) % len(workflows) if workflows else 0
            workflows = workflows[offset:] + workflows[:offset]
            store.set_state(cursor_key, offset + 1)
            report = {"generation": run["generation"], "workflows": []}
            for workflow in workflows:
                guard()
                result = {"platform": workflow.platform, "account": workflow.account, "stages": [], "status": "completed"}
                finalize = False
                for stage in sorted(workflow.stages, key=lambda value: value.priority):
                    guard()
                    if checkpoint:
                        value = checkpoint()
                        if inspect.isawaitable(value):
                            await value
                        guard()
                    if finalize and stage.lane == "work":
                        continue
                    if stage.requires_model and not remaining_model_calls():
                        result["stages"].append({"name": stage.name, "status": "model_quota"})
                        continue
                    if phase:
                        phase(stage.name, platform=workflow.platform)
                    with measure(store, stage.name, generation=run["generation"]):
                        value = stage.callback(workflow.data)
                        if inspect.isawaitable(value):
                            value = await value
                        guard()
                    flow = value if isinstance(value, Flow) else Flow.CONTINUE
                    result["stages"].append({"name": stage.name, "status": flow.value})
                    if flow == Flow.STOP:
                        result["status"] = "stopped"
                        break
                    finalize |= flow == Flow.FINALIZE
                report["workflows"].append(result)
            report["model_remaining"] = remaining_model_calls()
            return report
        finally:
            lock.release()
