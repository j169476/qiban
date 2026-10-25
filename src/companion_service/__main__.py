"""Local Python service entry. Startup never resumes role activity."""
import argparse
import asyncio
import json
from pathlib import Path

import uvicorn
from filelock import FileLock, Timeout

from companion_codex.host import CodexHost
from companion_codex.model import CodexModel
from companion_core.errors import AssistantError

from .config import open_store, urls
from .service import Service
from .web import create_app


async def serve(project):
    from bili_assistant.bilibili import Bilibili
    from bili_assistant.connector import BilibiliConnector
    from bili_assistant.engine import Engine

    config, store, secret = open_store(project)
    lock = FileLock(str(store.runtime / "service.lock"), timeout=0)
    # Coalesce adjacent nested checks only; failures never reuse an old result.
    host = CodexHost(Path(project), active_cache_seconds=0.1)
    try:
        lock.acquire()
        adapter = Bilibili(store)
        connector = BilibiliConnector(Engine(store, host, adapter))
        from companion_qq.module import configured_module as configured_qq
        from companion_wechat.module import configured_module

        wechat = configured_module(store, project, config["id"])
        qq = configured_qq(store, project)
        adapters = [connector] + ([wechat] if wechat is not None else []) + ([qq] if qq is not None else [])
        service = Service(store, CodexModel(), host, adapters=adapters, bridge_secret=secret,
                          platform_models={p: CodexModel(model="gpt-6.1-sol") for p in ("qq", "wechat")})
        service.installation_id = config["id"]
        from .config import secrets_for
        from .recovery import wrap

        service.recovery_export = lambda password: wrap(secrets_for(config)["key"], password)
        addresses = urls(config)
        servers = [uvicorn.Server(uvicorn.Config(
            create_app(service, kind=kind, origin=addresses[kind], counterpart=addresses["role" if kind == "admin" else "admin"]),
            host="127.0.0.1", port=port, access_log=False, server_header=False, log_level="error", limit_concurrency=32,
            timeout_keep_alive=5)) for kind, port in config["ports"].items()]
        await service.open()
        try:
            from .watchdog import Supervisor

            service.supervisor = Supervisor(service, project)
            service.supervisor.start()
            # One signal owner; secondary servers use Uvicorn's internal serving
            # routine so they cannot install competing process signal handlers.
            with servers[0].capture_signals():
                tasks = [asyncio.create_task(server._serve()) for server in servers]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for server in servers:
                    server.should_exit = True
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            try:
                await service.close()
            finally:
                if service.supervisor:
                    await asyncio.to_thread(service.supervisor.close)
    finally:
        host.close()
        lock.release()
        store.database.close()


def main():
    parser = argparse.ArgumentParser(description="Companion local service")
    parser.add_argument("command", choices=["serve", "install", "restore-new", "watch", "install-status", "install-finalize"])
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--postgres-bundle", type=Path)
    parser.add_argument("--seed-only", action="store_true", help="只迁移人物卡和明确引用的基础记忆，保留配置与动作额度账本")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--recovery-key", type=Path)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--parent-born", type=int)
    parser.add_argument("--instance")
    args = parser.parse_args()
    if args.seed_only and args.command != "install":
        parser.error("--seed-only is supported only by install")
    try:
        if args.command in {"install", "restore-new"}:
            from .install import install

            if not args.postgres_bundle:
                parser.error("install requires --postgres-bundle")
            recovery = None
            if args.command == "restore-new":
                import getpass

                from companion_core.postgres_transfer import MAX_BACKUP

                from .recovery import unwrap

                if not args.backup or not args.recovery_key:
                    parser.error("restore-new requires --backup and --recovery-key")
                if args.backup.stat().st_size > MAX_BACKUP or args.recovery_key.stat().st_size > 300:
                    raise AssistantError("recovery_limit", "恢复文件超过大小限制")
                # Interactive local entry only. Passwords are never CLI arguments.
                key = unwrap(args.recovery_key.read_bytes(), getpass.getpass("Recovery passphrase: "))
                recovery = (args.backup.read_bytes(), key)
            print(json.dumps(install(args.project, args.postgres_bundle, recovery=recovery,
                                     seed_only=args.seed_only), ensure_ascii=False))
        elif args.command in {"install-status", "install-finalize"}:
            from .installation import Journal, finalize

            value = Journal(args.project).status() if args.command == "install-status" else finalize(args.project)
            print(json.dumps(value, ensure_ascii=False))
        elif args.command == "watch":
            import re

            from .watchdog import watch

            if (not args.parent_pid or not args.parent_born or not args.instance
                    or not re.fullmatch(r"[a-f0-9]{32}", args.instance)):
                parser.error("watch requires a verified parent process and instance")
            watch(args.project, args.parent_pid, args.parent_born, args.instance)
        else:
            asyncio.run(serve(args.project))
    except (AssistantError, Timeout) as exc:
        print(json.dumps({"error": exc.code if isinstance(exc, AssistantError) else "service_already_running"}))
        raise SystemExit(2) from None
    except Exception:
        # Driver errors can contain connection credentials or private payloads.
        print(json.dumps({"error": "service_internal_error", "message": "服务未完成，角色不会自动恢复"}, ensure_ascii=False))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
