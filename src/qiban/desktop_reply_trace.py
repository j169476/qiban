"""Read-only reply processing trace inside the desktop app.

The trace is written by ``companion_core.reply_trace`` while the runtime handles a
reply. It holds the processing stages, timings, outcome and the resource versions
that were supplied; it never stores prompt text, conversation originals or model
reasoning. This module only reads that record through the existing
``listing``/``detail`` functions, so the key format, the ~30-day / ~500-record
retention and the two-part source filtering stay exactly as they are.

Every call needs a real current administrator request: the desktop lock, the
management session and the installation owner are checked again here, and the
detail re-filters each source against that same request through
``AccessService.can_read``. Nothing is written: no record, no policy, no
``desktop-request`` idempotency row, no model call, no platform action, and there
is no export, delete or retention edit.

The list has no cursor by design: it always returns the newest ``limit`` records
(1-100, default 30) so the page can state "最近 N 条" instead of pretending to page
through the whole history.
"""
from __future__ import annotations

from companion_core.errors import AssistantError
from companion_core.reply_trace import detail as core_detail
from companion_core.reply_trace import listing as core_listing

DEFAULT = 30
MAXIMUM = 100
IDENTIFIER = 100
MEANING = ("程序处理记录：只包含处理阶段、耗时、结果和来源版本，不含提示词原文、"
           "聊天原文或模型内部推理；不提供导出、删除或保留期修改。")


def _limit(value):
    # Same bound and the same strict integer check as the core listing.
    if type(value) is not int or not 1 <= value <= MAXIMUM:
        raise AssistantError("reply_trace_invalid", "最近记录数量必须是 1 至 100 的整数。")
    return value


def _identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= IDENTIFIER:
        raise AssistantError("reply_trace_invalid", "回复处理记录标识无效，请重新读取最近记录。")


def listing(manager, request, limit=DEFAULT):
    """The newest records only; the page offers a recent-count control, not paging."""
    manager.authority.require(request, administrator=True)
    rows = core_listing(manager.store, _limit(limit))
    # A lock or session change during the read must not hand back a page.
    manager.authority.require(request, administrator=True)
    return {"items": rows, "limit": limit, "count": len(rows), "recent_only": True, "meaning": MEANING}


def detail(manager, request, identifier):
    """One record with its sources filtered again by the current read permission."""
    manager.authority.require(request, administrator=True)
    _identifier(identifier)
    record = core_detail(manager.store, identifier, manager.admin.access, request)
    manager.authority.require(request, administrator=True)
    return record
