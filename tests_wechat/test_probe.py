"""Synthetic P0 observations; these tests never attach to a real desktop."""
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion_wechat.probe import (
    DesktopSnapshot,
    ProbeUnavailable,
    Structure,
    TargetSnapshot,
    TargetSpec,
    run_probe,
)
from companion_wechat.providers import uia


class FakeBackend:
    def __init__(self, snapshot=None, failure=None):
        self.value = snapshot or DesktopSnapshot()
        self.failure = failure
        self.calls = 0

    def snapshot(self, targets):
        self.calls += 1
        if self.failure:
            raise self.failure
        return self.value


@pytest.mark.parametrize("snapshot,status", [
    (DesktopSnapshot(), "client_not_running"),
    (DesktopSnapshot(process_count=1), "main_window_not_visible"),
    (DesktopSnapshot(process_count=1, unrecognized_windows=1), "unrecognized_window_structure"),
    (DesktopSnapshot(process_count=1, login_windows=1), "login_required"),
    (DesktopSnapshot(process_count=1, main_windows=1), "ui_tree_unavailable"),
    (DesktopSnapshot(process_count=1, main_windows=1, main_structure=Structure(truncated=True)),
     "ui_structure_inconclusive"),
    (DesktopSnapshot(process_count=2, main_windows=2), "multiple_main_windows"),
    (DesktopSnapshot(process_count=1, main_windows=1, main_structure=Structure(session_list=True)),
     "ui_visible_identity_unverified"),
])
def test_environment_states_are_not_reported_as_successful_account_binding(snapshot, status):
    result = run_probe(backend=FakeBackend(snapshot), system="Windows")
    assert result["status"] == status
    assert result["account"]["verified"] is False
    assert result["read_only"] is True
    assert result["messages_sent"] == 0
    assert result["message_content_collected"] is False


def test_unsupported_platform_does_not_construct_or_call_provider():
    backend = FakeBackend()
    assert run_probe(backend=backend, system="Linux")["status"] == "unsupported_platform"
    assert backend.calls == 0


@pytest.mark.parametrize("error,status", [
    (ProbeUnavailable("dependency_missing"), "dependency_missing"),
    (ProbeUnavailable("dependency_unavailable"), "dependency_unavailable"),
    (RuntimeError("SECRET private chat C:/Users/person/secret"), "probe_failed"),
    (ProbeUnavailable("SECRET"), "probe_failed"),
])
def test_exception_details_never_escape(error, status):
    result = run_probe(backend=FakeBackend(failure=error), system="Windows")
    assert result["status"] == status
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("item,status", [
    (TargetSnapshot("friend"), "target_not_open"),
    (TargetSnapshot("friend", 2), "target_ambiguous"),
    (TargetSnapshot("friend", 1), "target_ui_tree_unavailable"),
    (TargetSnapshot("friend", 1, Structure(truncated=True)), "target_structure_inconclusive"),
    (TargetSnapshot("friend", 1, Structure(message_lists=1, message_items=5)),
     "target_visible_identity_unverified"),
    (TargetSnapshot("friend", failed=True), "target_probe_failed"),
])
def test_target_diagnostics_never_promote_title_or_declared_kind_to_identity(item, status):
    target = TargetSpec("friend", "private", "SENSITIVE TITLE")
    snapshot = DesktopSnapshot(process_count=1, main_windows=1, targets=(item,))
    result = run_probe([target], backend=FakeBackend(snapshot), system="Windows")
    value = result["targets"][0]
    assert value["status"] == status
    assert value["identity_verified"] is False
    assert value["kind_verified"] is False
    assert "SENSITIVE TITLE" not in json.dumps(result)
    assert "SENSITIVE TITLE" not in repr(target)


@pytest.mark.parametrize("arguments", [
    ("个人真实姓名", "private", "title"), ("friend", "public", "title"),
    ("friend", "private", ""), ("friend", "private", " " * 3),
    ("friend", "private", "x" * 201),
])
def test_invalid_target_specs_fail_before_desktop_access(arguments):
    with pytest.raises(ValueError):
        TargetSpec(*arguments)


def test_duplicate_aliases_and_unbounded_target_lists_are_rejected():
    target = TargetSpec("friend", "private", "title")
    for targets in ([target, target], [replace(target, alias=f"target{i}") for i in range(9)], ["not a target"]):
        with pytest.raises(ValueError, match="invalid_probe_targets"):
            run_probe(targets, backend=FakeBackend(), system="Windows")


class Node:
    def __init__(self, kind="Custom", class_name="", title="", children=(), forbid_text=False):
        self.element_info = SimpleNamespace(control_type=kind)
        self.name, self.title = class_name, title
        self.values = list(children)
        self.forbid_text = forbid_text
        self.child_calls = 0

    def class_name(self):
        return self.name

    def window_text(self):
        assert not self.forbid_text, "message text must never be inspected"
        return self.title

    def children(self, control_type=None):
        self.child_calls += 1
        if control_type:
            return [value for value in self.values if value.element_info.control_type == control_type]
        return self.values


def test_default_inspection_skips_all_list_items_including_unrelated_chats():
    secret = Node("ListItem", title="secret message", forbid_text=True)
    messages = Node("List", title="消息", children=[secret])
    sessions = Node("List", title="会话", children=[secret])
    edit = Node("Edit", uia.INPUT, title="unsent secret", forbid_text=True)
    root = Node(children=[messages, sessions, edit])
    result = uia.inspect_structure(root)
    assert result.available and result.session_list
    assert result.message_lists == result.input_fields == 1
    assert result.message_items == 0
    assert messages.child_calls == sessions.child_calls == secret.child_calls == 0


def test_selected_target_only_counts_items_without_reading_senders_text_or_descendants():
    secret = Node("ListItem", title="secret message", forbid_text=True)
    messages = Node("List", title="Messages", children=[secret, secret])
    unrelated = Node("List", title="Contact list", children=[secret])
    root = Node(children=[messages, unrelated])
    result = uia.inspect_structure(root, target=True)
    assert result.message_lists == 1 and result.message_items == 2
    assert secret.child_calls == unrelated.child_calls == 0


def test_structure_walk_is_bounded_for_wide_or_cyclic_provider_trees():
    broad = Node(children=[Node() for _ in range(50)])
    assert uia.inspect_structure(broad, max_nodes=3).nodes == 3
    assert uia.inspect_structure(broad, max_nodes=3).truncated
    cycle = Node()
    cycle.values = [cycle]
    result = uia.inspect_structure(cycle, max_depth=2)
    assert result.nodes == 3 and result.truncated


def test_real_backend_selects_only_wechat_process_windows_and_does_not_inspect_other_targets(monkeypatch):
    selected = Node("Window", uia.CHAT, "TEST friend", [Node("List", title="消息")])
    unrelated = Node("Window", uia.CHAT, "OTHER family", [Node(forbid_text=True)])
    main = Node("Window", uia.MAIN, children=[Node("List", title="会话")])
    filters = []

    class Desktop:
        def __init__(self, backend):
            assert backend == "uia"

        def windows(self, **kwargs):
            filters.append(kwargs)
            return [main, selected, unrelated]

    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=Desktop))
    monkeypatch.setattr(uia, "version", lambda package: "0.6.9")
    monkeypatch.setattr(uia, "_processes", lambda: [42])
    monkeypatch.setattr(uia, "_file_version", lambda pid: "4.1.9.1")
    result = uia.WindowsUIABackend().snapshot((TargetSpec("friend", "private", "TEST friend"),))
    assert filters[0]["process"] == 42 and filters[0]["visible_only"]
    assert result.targets[0].candidates == 1 and result.targets[0].structure.available
    assert unrelated.child_calls == 0


def test_ambiguous_title_never_traverses_either_candidate(monkeypatch):
    candidates = [Node("Window", uia.CHAT, "TEST same") for _ in range(2)]
    desktop = SimpleNamespace(windows=lambda **kwargs: candidates)
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda **kwargs: desktop))
    monkeypatch.setattr(uia, "version", lambda package: "0.6.9")
    monkeypatch.setattr(uia, "_processes", lambda: [42])
    monkeypatch.setattr(uia, "_file_version", lambda pid: "")
    result = uia.WindowsUIABackend().snapshot((TargetSpec("family", "group", "TEST same"),))
    assert result.targets[0].candidates == 2
    assert all(w.child_calls == 0 for w in candidates)


def test_unrecognized_client_window_is_counted_without_reading_its_name_or_children(monkeypatch):
    window = Node("Window", "NewWeixinWindowClass", title="SECRET", forbid_text=True)
    filters = []

    def windows(**kwargs):
        filters.append(kwargs)
        return [window]

    desktop = SimpleNamespace(windows=windows)
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda **kwargs: desktop))
    monkeypatch.setattr(uia, "version", lambda package: "0.6.9")
    monkeypatch.setattr(uia, "_processes", lambda: [42])
    monkeypatch.setattr(uia, "_file_version", lambda pid: "4.1.15.13")
    report = run_probe(backend=uia.WindowsUIABackend(), system="Windows")
    assert all("class_name_re" not in item for item in filters)
    assert report["status"] == "unrecognized_window_structure"
    assert report["client"]["unrecognized_windows"] == report["client"]["visible_windows"] == 1
    assert window.child_calls == 0 and "SECRET" not in json.dumps(report)


@pytest.mark.parametrize("semantic,truncated,status", [
    (False, False, "render_surface_only"),
    (True, False, "unrecognized_window_structure"),
    (False, True, "unrecognized_window_structure"),
])
def test_render_structure_does_not_become_authenticated_main_window(semantic, truncated, status):
    snapshot = DesktopSnapshot(process_count=1, unrecognized_windows=1, render_surfaces=1,
                               render_structure=Structure(nodes=3, truncated=truncated, session_list=semantic))
    report = run_probe(backend=FakeBackend(snapshot), system="Windows")
    assert report["status"] == status
    assert report["account"] == {"verified": False, "status": "not_observed"}
    assert report["client"]["main_windows"] == 0
    assert report["render_ui"]["nodes_examined"] == 3
    assert report["render_ui"]["available"] is semantic


@pytest.mark.parametrize("has_render_pane,has_messages,status", [
    (True, False, "render_surface_only"),
    (True, True, "unrecognized_window_structure"),
    (False, False, "unrecognized_window_structure"),
])
def test_observed_qt_render_surface_is_inspected_without_names_or_message_items(
        monkeypatch, has_render_pane, has_messages, status):
    secret = Node("ListItem", title="SECRET actual content", forbid_text=True)
    messages = Node("List", title="消息", children=[secret])
    renderer = Node("Pane", uia.RENDER_PANE, title="SECRET renderer", forbid_text=True,
                    children=[messages] if has_messages else [])
    pane = Node("Pane", "Qt51514QWindowIcon", title="SECRET pane", forbid_text=True)
    root = Node("Window", "Qt51514QWindowIcon", title="SECRET title", forbid_text=True,
                children=[pane, renderer] if has_render_pane else [pane])
    desktop = SimpleNamespace(windows=lambda **kwargs: [root])
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda **kwargs: desktop))
    monkeypatch.setattr(uia, "version", lambda package: "0.6.9")
    monkeypatch.setattr(uia, "_processes", lambda: [42])
    monkeypatch.setattr(uia, "_file_version", lambda pid: "4.1.15.13")
    report = run_probe(backend=uia.WindowsUIABackend(), system="Windows")
    assert report["status"] == status
    assert report["client"]["render_surfaces"] == int(has_render_pane)
    assert report["client"]["main_windows"] == report["client"]["login_windows"] == 0
    assert report["account"]["verified"] is False
    assert report["render_ui"]["visible_message_items"] == 0
    assert secret.child_calls == messages.child_calls == 0
    assert "SECRET" not in json.dumps(report)


def test_render_surface_with_other_unknown_windows_is_still_inconclusive():
    snapshot = DesktopSnapshot(process_count=1, unrecognized_windows=2, render_surfaces=1,
                               render_structure=Structure(nodes=3))
    report = run_probe(backend=FakeBackend(snapshot), system="Windows")
    assert report["status"] == "unrecognized_window_structure"
