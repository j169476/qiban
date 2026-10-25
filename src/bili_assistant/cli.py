import argparse
import asyncio
import json
import sys
from pathlib import Path

from .api import TOOLS, call
from .engine import Engine
from .errors import AssistantError
from .host import CodexHost
from .storage import Store


def parser():
    root = argparse.ArgumentParser(description="Codex B站助手")
    root.add_argument("--project", type=Path, default=Path.cwd())
    root.add_argument("--data", type=Path)
    sub = root.add_subparsers(dest="command", required=True)
    for command in ("init", "validate", "rebuild-index", "backup", "doctor", "login", "serve", "notifications", "migrate-core"):
        sub.add_parser(command)
    export = sub.add_parser("export")
    export.add_argument("destination", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("destination", type=Path)
    invoke = sub.add_parser("call")
    invoke.add_argument("tool", choices=TOOLS)
    invoke.add_argument("--input", type=Path, help="UTF-8 JSON 参数文件；不传则使用空对象")
    daemon = sub.add_parser("daemon")
    daemon.add_argument("--generation", required=True)
    supervisor = sub.add_parser("supervise")
    supervisor.add_argument("--generation", required=True)
    sub.add_parser("schemas")
    return root


async def run(args):
    from companion_service.config import metadata

    if metadata(args.project) is not None:
        from companion_service import bridge

        if args.command == "call":
            from jsonschema import Draft202012Validator

            arguments = json.loads(args.input.read_text(encoding="utf-8-sig")) if args.input else {}
            if list(Draft202012Validator(TOOLS[args.tool][1]).iter_errors(arguments)):
                raise AssistantError("invalid_arguments", "工具参数不符合定义")
            if args.tool == "config_patch":
                arguments = {**arguments, "changes": arguments["patch"]}
                arguments.pop("patch")
            return await asyncio.to_thread(bridge.call, args.project, args.tool, arguments)
        if args.command == "serve":
            from companion_service.mcp import serve

            return await serve(args.project)
        if args.command in {"doctor", "validate"}:
            return {"backend": "postgres_service", "ready": bridge.ready(metadata(args.project)),
                    "web": bridge.urls(metadata(args.project)), "role_started": False}
        raise AssistantError("service_entry_required", "当前项目已迁移至本机服务，请使用管理网页或后台管理聊天")
    if args.command == "restore":
        from .backups import restore

        return restore(args.archive, args.destination)
    store = Store(args.data or args.project / "bili-assistant-data")
    if args.command == "init":
        return store.initialize()
    if args.command == "validate":
        return store.validate_all()
    if args.command == "migrate-core":
        from .connector import migrate_legacy
        if store.state("run", {}).get("status") == "active":
            raise AssistantError("stop_required", "先暂停角色再迁移")
        store.initialize()
        return migrate_legacy(store)
    if args.command == "schemas":
        return store.profile.schemas
    if args.command == "rebuild-index":
        return store.rebuild_index()
    if args.command == "backup":
        return store.backup(force=True)
    if args.command == "export":
        return store.export(args.destination)
    if args.command == "login":
        from .credentials import login

        return await login(store)
    host = CodexHost(args.project)
    try:
        if args.command == "doctor":
            from .scheduler import readiness

            engine = Engine(store, host)
            result = {"files": store.validate_all(), "identity_available": bool(host.thread_id)}
            from .health import status

            result["health"] = status(engine)
            try:
                result["host"] = host.status()
            except AssistantError as exc:
                result["host"] = exc.as_dict()
            try:
                result["background"] = readiness(engine)
            except AssistantError as exc:
                result["background"] = exc.as_dict()
            return result
        from .bilibili import Bilibili

        engine = Engine(store, host, Bilibili(store))
        if args.command == "call":
            arguments = json.loads(args.input.read_text(encoding="utf-8-sig")) if args.input else {}
            return await call(engine, args.tool, arguments)
        if args.command == "serve":
            from .server import serve

            await serve(engine)
        if args.command == "daemon":
            from .scheduler import Scheduler

            await Scheduler(engine, args.generation).run()
        if args.command == "supervise":
            from .supervisor import supervise

            await supervise(engine, args.generation)
        if args.command == "notifications":
            from .health import notification_loop

            await notification_loop(engine)
        return None
    finally:
        host.close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        result = asyncio.run(run(parser().parse_args()))
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except AssistantError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        raise SystemExit(2) from None
    except (OSError, ValueError):
        print('{"error":"invalid_input_or_io","message":"输入文件或文件访问失败"}')
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
