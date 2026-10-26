"""Public-workspace filtering must preserve the underlying stage's control flow."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bili_assistant import governed_workflow
from companion_core.orchestration import Flow, Stage, Workflow


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", [None, Flow.CONTINUE, Flow.STOP, Flow.FINALIZE])
async def test_selection_context_preserves_control_flow(monkeypatch, signal):
    readable = Mock(return_value={"public-resource"})
    workspace = SimpleNamespace(repository=SimpleNamespace(readable_resources=readable),
                                access=object(), request=object())

    class SyntheticWorkflow:
        def __init__(self, scheduler):
            pass

        async def prepare(self, state):
            if signal in (Flow.STOP, Flow.FINALIZE):
                return signal
            state["fresh"] = [
                {"kind": "dm_reply", "text": "private-canary"},
                {"kind": "reply", "text": "public", "relationship_context": "private-context-canary"},
            ]
            return signal

        def definition(self):
            return Workflow("bilibili", "123", (Stage("selection_context", self.prepare, 40, "prepare"),))

    monkeypatch.setattr(governed_workflow, "PublicWorkspace", lambda store: workspace)
    monkeypatch.setattr(governed_workflow, "CoreScheduler", lambda engine, core: object())
    monkeypatch.setattr(governed_workflow, "BilibiliWorkflow", SyntheticWorkflow)
    workflow = governed_workflow.definition(SimpleNamespace(account="123"), SimpleNamespace(store=object()))
    stage = next(item for item in workflow.stages if item.name == "selection_context")

    assert await stage.callback(workflow.data) is signal
    if signal in (Flow.STOP, Flow.FINALIZE):
        assert "fresh" not in workflow.data
        readable.assert_not_called()
    else:
        assert workflow.data["fresh"] == [{"kind": "reply", "text": "public"}]
        readable.assert_called_once_with(workspace.access, workspace.request)
        assert workspace.readable == {"public-resource"}
