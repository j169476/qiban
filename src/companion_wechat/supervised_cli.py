"""Explicit short role test over a private parent/child supervision pipe.

Run only as a child with anonymous stdin/stdout pipes. No desktop automation is
implemented here. Candidate replies exist only in process memory and that pipe;
public status frames contain fixed states, a test nonce, times and counters.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import secrets
import sys
from pathlib import Path

from companion_core.errors import AssistantError

from .contracts import require
from .db_cli import load_config
from .receiver_cli import open_collector_store
from .supervised_pipe import PIPE_ERRORS, PrivateJsonPipe, SupervisedPipeOperator, require_private_stdio

_STATES = {"new", "ready", "processing", "complete", "deadline", "interrupted", "blocked", "failed"}
_COUNTERS = {"handled", "succeeded", "uncertain", "failed", "cancelled", "no_action"}
_ERRORS = PIPE_ERRORS | {"wechat_role_configuration", "wechat_supervised_dependency_missing",
                       "host_identity_missing", "host_unavailable", "host_timeout", "host_protocol_error",
                       "host_state_unknown", "wrong_project", "chat_archived", "not_owner", "not_running",
                       "wechat_role_already_running", "wechat_role_already_active", "wechat_role_collector_busy",
                       "wechat_role_scope", "wechat_role_not_ready", "wechat_role_expired", "wechat_role_revoked",
                       "wechat_role_generation", "wechat_role_binding_changed", "wechat_role_source_blocked",
                       "wechat_role_failed", "wechat_send_store_unencrypted", "collector_service_dependency_missing",
                       "service_missing", "service_config", "service_vault", "database_runtime",
                       "database_timeout", "database_unavailable", "database_error", "storage_integrity",
                       "wechat_disconnected", "wechat_account_mismatch", "wechat_receiver_owner_conflict",
                       "wechat_receipt_unavailable", "wechat_receipt_invalid", "wechat_receipt_disconnected",
                       "wechat_supervised_failed", "configuration_invalid", "model_timeout", "model_output_size",
                       "model_auth", "model_protocol", "model_process_exit", "model_failed", "model_unavailable",
                       "model_isolation", "model_config", "model_tool_denied", "model_output_invalid"}


def public_status(value):
    """Never forward arbitrary exception text, event bodies or identity fields."""
    raw = value if isinstance(value, dict) else {}
    status = raw.get("status")
    result = {"schema_version": 1, "provider": "wechat_supervised_role",
              "status": status if status in _STATES else "failed",
              "role_stopped": raw.get("role_stopped") is True,
              "keys_exported": False, "plaintext_database_created": False}
    for key in _COUNTERS:
        number = raw.get(key, 0)
        result[key] = number if type(number) is int and 0 <= number <= 2 else 0
    elapsed = raw.get("elapsed_seconds", 0.0)
    result["elapsed_seconds"] = (round(elapsed, 3) if type(elapsed) in (int, float)
                                 and math.isfinite(elapsed) and 0 <= elapsed <= 3600 else 0.0)
    test = raw.get("test")
    if isinstance(test, dict):
        nonce = test.get("nonce")
        safe = {"status": test.get("status") if test.get("status") in {"new", "prepared", "active", "revoked"} else "new"}
        if isinstance(nonce, str) and re.fullmatch(r"XLD-ROLE-[A-F0-9]{24}", nonce):
            safe["nonce"] = nonce
        # A constrained ISO timestamp is sufficient for display; no identity can
        # be hidden in arbitrary nested status strings.
        deadline = test.get("deadline_at")
        if isinstance(deadline, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+00:00", deadline):
            safe["deadline_at"] = deadline
        for field in ("admitted", "reserved", "completed"):
            counts = test.get(field, {})
            safe[field] = {alias: int(isinstance(counts, dict) and type(counts.get(alias)) is int
                                      and counts[alias] == 1) for alias in ("owner_main", "family")}
        result["test"] = safe
    code = raw.get("code")
    if isinstance(code, str) and code in _ERRORS:
        result["code"] = code
    return result


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise AssistantError("wechat_role_configuration", "监督短测参数无效")


def parser():
    value = Parser(description=__doc__)
    value.add_argument("--project", type=Path, default=Path.cwd())
    value.add_argument("--locations", type=Path, required=True)
    value.add_argument("--targets", type=Path, required=True)
    value.add_argument("--worker-python", type=Path)
    value.add_argument("--duration-seconds", type=int, default=180)
    value.add_argument("--poll-seconds", type=float, default=1.0)
    value.add_argument("--model-effort", choices=("low", "medium", "high", "xhigh"))
    value.add_argument("--model", help="仅覆盖本次短测，不写全局配置")
    return value


def validate_options(args):
    require(getattr(args, "model_effort", None) in (None, "low", "medium", "high", "xhigh"),
            "wechat_role_configuration")
    model = getattr(args, "model", None)
    require(model is None or isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", model),
            "wechat_role_configuration")
    require(type(args.duration_seconds) is int and 5 <= args.duration_seconds <= 600
            and type(args.poll_seconds) in (int, float) and math.isfinite(args.poll_seconds)
            and 0.5 <= args.poll_seconds <= 10, "wechat_role_configuration")
    # Do not accept a command line override or invent a replacement controller.
    thread = os.environ.get("CODEX_THREAD_ID")
    require(isinstance(thread, str) and re.fullmatch(r"[0-9a-fA-F-]{32,36}", thread), "host_identity_missing")


async def run_supervised(receiver, model, host, pipe, *, duration_seconds=180, poll_seconds=1.0):
    """Composition boundary; real ports are supplied, never replaced with fakes."""
    from companion_core.runtime import Companion

    from .gateway import PermitAuthority
    from .receipt_client import DatabaseReceiptClient
    from .role import RoleAdapter, RoleSession
    from .role_runtime import RoleTestRunner
    from .supervised_sender import SupervisedSenderGateway

    adapter = sender = session = operator = None
    try:
        require(getattr(receiver.store, "backend", None) == "postgres" and receiver.store.db_path is None,
                "wechat_send_store_unencrypted")
        require(host.identity() == os.environ.get("CODEX_THREAD_ID"), "host_identity_missing")
        if receiver.connector is None:
            await receiver.start()
        # Reserve time for real host/DB checks; the final UI grant stays <=2s.
        permits = PermitAuthority(secrets.token_bytes(32), lifetime_seconds=20.0)
        session = RoleSession(receiver.store, receiver.account, duration_seconds=duration_seconds)
        operator = SupervisedPipeOperator(pipe, DatabaseReceiptClient(receiver),
                                          prepare_timeout=min(180.0, duration_seconds))
        sender = SupervisedSenderGateway(receiver.store, receiver, permits, operator, session,
                                          prepare_timeout=min(180.0, duration_seconds), commit_timeout=20.0)
        adapter = RoleAdapter(receiver, sender, permits, session)
        core = Companion(receiver.store, model, host, [adapter], run_key="core-run:wechat")

        def emit(report):
            pipe.last_public_report = public_status(report)
            pipe.publish("status", {"report": pipe.last_public_report})

        return await RoleTestRunner(core, adapter).run(poll_seconds=poll_seconds, emit=emit)
    finally:
        try:
            if adapter is not None:
                await adapter.close()
            else:
                try:
                    if sender is not None:
                        await sender.close()
                    elif operator is not None:
                        await operator.close()
                finally:
                    try:
                        await receiver.close()
                    finally:
                        if session is not None:
                            session.close()
        finally:
            host.close()


async def _run(args, pipe):
    try:
        from companion_codex.host import CodexHost
        from companion_codex.model import CodexModel
    except ImportError:
        raise AssistantError("wechat_supervised_dependency_missing", "监督短测需要完整项目依赖") from None

    from .receiver import DatabaseGateway

    await pipe.handshake()

    async def operation():
        config = load_config(args.locations, args.targets)
        kwargs = {}
        if args.worker_python is not None:
            from .receiver_transport import SubprocessTransport

            path = args.worker_python.resolve(strict=True)
            require(path.is_file() and not str(path).startswith(("\\\\", "//")), "wechat_role_configuration")
            kwargs["transport_factory"] = lambda: SubprocessTransport(python=str(path))
        with open_collector_store(args.project) as (store, account):
            host = CodexHost(args.project, active_cache_seconds=0.1)
            try:
                receiver = DatabaseGateway(store, config, account=account, **kwargs)
                effort = getattr(args, "model_effort", None)
                selected_model = getattr(args, "model", None)
                overrides = {key: value for key, value in {"reasoning_effort": effort, "model": selected_model}.items()
                             if value is not None}
                model = CodexModel(**overrides)
                result = await run_supervised(receiver, model, host, pipe,
                                              duration_seconds=args.duration_seconds, poll_seconds=args.poll_seconds)
            finally:
                host.close()
        return result

    return await pipe.supervise(operation())


async def _stdio(args, reader, writer):
    pipe = PrivateJsonPipe(reader, writer)
    try:
        try:
            result = await _run(args, pipe)
            pipe.publish("result", {"report": public_status(result)})
            return 0 if result.get("status") in {"complete", "deadline"} else 2
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            code = code if isinstance(code, str) and code in _ERRORS else "wechat_supervised_failed"
            try:
                previous = getattr(pipe, "last_public_report", {})
                pipe.publish("result", {"report": public_status({**previous, "status": "blocked", "code": code})})
            except Exception:
                pass
            return 2
    finally:
        await pipe.close()


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        validate_options(args)
        reader, writer = sys.stdin.buffer, sys.stdout.buffer
        require_private_stdio(reader, writer)
        return asyncio.run(_stdio(args, reader, writer))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        code = getattr(exc, "code", None)
        code = code if isinstance(code, str) and code in _ERRORS else "wechat_supervised_failed"
        # Startup diagnostics contain no private candidate and never a raw error.
        print(json.dumps({"status": "blocked", "code": code}), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
