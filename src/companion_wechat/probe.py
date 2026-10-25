"""Read-only, redacted diagnostics; visibility never proves account identity.

The real provider is imported only inside ``run_probe``. Call this function in a
short-lived subprocess: a UI Automation provider can hang in a native COM call,
so an in-process timer is not a reliable timeout.
"""
from __future__ import annotations

import platform
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True)
class TargetSpec:
    """An explicitly chosen open window; title is not a verified identity."""

    alias: str
    kind: str
    title: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.alias, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", self.alias):
            raise ValueError("invalid_target_alias")
        if self.kind not in {"private", "group"}:
            raise ValueError("invalid_target_kind")
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 200:
            raise ValueError("invalid_target_title")


@dataclass(frozen=True)
class Structure:
    nodes: int = 0
    truncated: bool = False
    session_list: bool = False
    message_lists: int = 0
    message_items: int = 0
    input_fields: int = 0

    @property
    def available(self):
        return self.session_list or self.message_lists > 0 or self.input_fields > 0


@dataclass(frozen=True)
class TargetSnapshot:
    alias: str
    candidates: int = 0
    structure: Structure = field(default_factory=Structure)
    failed: bool = False


@dataclass(frozen=True)
class DesktopSnapshot:
    process_count: int = 0
    versions: tuple[str, ...] = ()
    main_windows: int = 0
    login_windows: int = 0
    chat_windows: int = 0
    main_structure: Structure = field(default_factory=Structure)
    targets: tuple[TargetSnapshot, ...] = ()
    dependency_version: str = ""
    unrecognized_windows: int = 0
    render_surfaces: int = 0
    render_structure: Structure = field(default_factory=Structure)


class ProbeUnavailable(Exception):
    """Known diagnostics without underlying exception text or private paths."""

    def __init__(self, code):
        self.code = code if code in {"dependency_missing", "dependency_unavailable"} else "probe_failed"
        super().__init__(self.code)


class ProbeBackend(Protocol):
    def snapshot(self, targets: tuple[TargetSpec, ...]) -> DesktopSnapshot: ...


def _structure(value):
    return {"available": bool(value.available), "nodes_examined": value.nodes,
            "truncated": bool(value.truncated), "session_list": bool(value.session_list),
            "message_lists": value.message_lists, "visible_message_items": value.message_items,
            "input_fields": value.input_fields}


def run_probe(targets=(), *, backend: ProbeBackend | None = None, system=None):
    """Inspect structure only; never launch, activate or interact with WeChat.

    Targets must already be open as independent chat windows. Names, account
    identifiers, paths, message text and exception text never enter this report.
    Tests may inject a backend with already-redacted structural observations.
    """
    targets = tuple(targets)
    if (len(targets) > 8 or any(not isinstance(t, TargetSpec) for t in targets)
            or len({t.alias for t in targets}) != len(targets)):
        raise ValueError("invalid_probe_targets")
    report = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "provider": "pywinauto-uia-readonly",
        "environment": {"python": platform.python_version(), "os_release": platform.release(),
                        "os_version": platform.version(), "architecture": platform.machine()},
        "status": "probe_failed",
        "read_only": True,
        "messages_sent": 0,
        "message_content_collected": False,
        "account": {"verified": False, "status": "not_checked"},
        "targets": [],
        "limitations": ["visibility_is_not_identity", "ui_ids_are_not_server_message_ids",
                        "no_message_parsing_or_delivery_validation"],
    }
    current_system = system if system is not None else platform.system()
    if current_system != "Windows":
        report["status"] = "unsupported_platform"
        return report
    try:
        if backend is None:
            from .providers.uia import WindowsUIABackend

            backend = WindowsUIABackend()
        snapshot = backend.snapshot(targets)
    except ProbeUnavailable as exc:
        report["status"] = exc.code
        return report
    except Exception:
        # Native COM errors often include titles, paths or rendered controls.
        return report

    report["dependency_version"] = snapshot.dependency_version
    report["client"] = {"process_count": snapshot.process_count,
                        "versions": sorted(set(snapshot.versions)),
                        "main_windows": snapshot.main_windows,
                        "login_windows": snapshot.login_windows,
                        "independent_chat_windows": snapshot.chat_windows,
                        "unrecognized_windows": snapshot.unrecognized_windows,
                        "render_surfaces": snapshot.render_surfaces,
                        "visible_windows": snapshot.main_windows + snapshot.login_windows
                                           + snapshot.chat_windows + snapshot.unrecognized_windows}
    report["ui"] = _structure(snapshot.main_structure)
    report["render_ui"] = _structure(snapshot.render_structure)
    if not snapshot.process_count:
        report["status"] = "client_not_running"
    elif snapshot.login_windows and not snapshot.main_windows:
        report["status"] = "login_required"
    elif (not snapshot.main_windows and not snapshot.chat_windows and snapshot.render_surfaces
          and snapshot.render_surfaces == snapshot.unrecognized_windows
          and not snapshot.render_structure.available and not snapshot.render_structure.truncated):
        report["status"] = "render_surface_only"
    elif not snapshot.main_windows and snapshot.unrecognized_windows:
        report["status"] = "unrecognized_window_structure"
    elif not snapshot.main_windows:
        report["status"] = "main_window_not_visible"
    elif snapshot.main_windows > 1:
        report["status"] = "multiple_main_windows"
    elif not snapshot.main_structure.available:
        report["status"] = ("ui_structure_inconclusive" if snapshot.main_structure.truncated
                            else "ui_tree_unavailable")
    else:
        report["status"] = "ui_visible_identity_unverified"
    report["account"]["status"] = ("requires_explicit_verification" if snapshot.main_windows
                                    else "not_observed")
    observed = {t.alias: t for t in snapshot.targets}
    for target in targets:
        item = observed.get(target.alias, TargetSnapshot(target.alias))
        if item.failed:
            status = "target_probe_failed"
        elif item.candidates == 0:
            status = "target_not_open"
        elif item.candidates > 1:
            status = "target_ambiguous"
        elif not item.structure.available:
            status = "target_structure_inconclusive" if item.structure.truncated else "target_ui_tree_unavailable"
        else:
            status = "target_visible_identity_unverified"
        report["targets"].append({"alias": target.alias, "declared_kind": target.kind,
                                  "kind_verified": False, "identity_verified": False,
                                  "status": status, "window_candidates": item.candidates,
                                  "ui": _structure(item.structure)})
    return report
