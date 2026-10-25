"""Explicit standalone receive-only collector; no role start or send command."""
from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path

from companion_core.errors import AssistantError

from .collector import (
    Collector,
    base_report,
    collector_account,
    public_report,
    safe_code,
    validate_options,
    verify_store,
)
from .contracts import require
from .db_cli import load_config


@contextmanager
def open_collector_store(project):
    """CLI composition root for the existing installed, encrypted PostgreSQL.

    No role/content initialization or validation is called. Credentials never
    become command arguments or output. Python/provider copies cannot be
    promised to be fully erased when the process exits.
    """
    try:
        from bili_assistant.profiles import profile
        from companion_core.postgres import PostgresStore
        from companion_service.config import metadata, secrets_for
        from companion_service.managed_postgres import ensure
    except ImportError:
        raise AssistantError("collector_service_dependency_missing",
                             "实时采集命令需要安装本项目的本机服务与 PostgreSQL 依赖") from None

    store, secret = None, {}
    try:
        try:
            config = metadata(project)
            require(config is not None, "service_missing")
            ensure(config)
            secret = secrets_for(config)
            store = PostgresStore(config["data"], {**config["database"], "password": secret["database_password"]},
                                  secret["key"], profile())
            secret.clear()
            verify_store(store)
        except ImportError:
            raise AssistantError("collector_service_dependency_missing",
                                 "实时采集命令需要安装本项目的本机服务与 PostgreSQL 依赖") from None
        yield store, collector_account(config["id"])
    finally:
        secret.clear()
        if store is not None:
            store.database.close()


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise AssistantError("collector_configuration_invalid", "采集参数无效")


def parser():
    value = Parser(description=__doc__)
    value.add_argument("--project", type=Path, default=Path.cwd(), help="已初始化的当前项目目录")
    value.add_argument("--locations", type=Path, required=True, help="本地微信账号目录配置")
    value.add_argument("--targets", type=Path, required=True, help="已有两目标配置，必须禁用自动发送")
    timing = value.add_mutually_exclusive_group()
    timing.add_argument("--once", action="store_true", help="完成一次采集后退出")
    timing.add_argument("--duration-seconds", type=int, help="可选时限；省略时持续运行直到 Ctrl+C")
    value.add_argument("--poll-seconds", type=float, default=1.0)
    value.add_argument("--worker-python", type=Path, help="可选：已安装只读数据库依赖的 Python 可执行文件")
    return value


async def _run(args, emit):
    from .receiver import DatabaseGateway

    config = load_config(args.locations, args.targets)
    kwargs = {}
    if args.worker_python is not None:
        from .receiver_transport import SubprocessTransport

        python = args.worker_python.resolve(strict=True)
        require(python.is_file() and not str(python).startswith(("\\\\", "//")), "collector_configuration_invalid")
        kwargs["transport_factory"] = lambda: SubprocessTransport(python=str(python))
    with open_collector_store(args.project) as (store, account):
        gateway = DatabaseGateway(store, config, account=account, **kwargs)
        collector = Collector(store, gateway, account)
        return await collector.run(once=args.once, duration_seconds=args.duration_seconds,
                                   poll_seconds=args.poll_seconds, emit=emit)


def main(argv=None):
    latest = base_report()

    def emit(value):
        nonlocal latest
        latest = public_report(value)
        print(json.dumps(latest, ensure_ascii=False, allow_nan=False), flush=True)

    try:
        args = parser().parse_args(argv)
        validate_options(args.once, args.duration_seconds, args.poll_seconds)
        result = asyncio.run(_run(args, emit))
        return 0 if result["status"] == "complete" else 2
    except KeyboardInterrupt:
        emit({**latest, "status": "interrupted"})
        return 130
    except Exception as exc:
        emit({**latest, "status": safe_code(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
