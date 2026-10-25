"""Offline configuration and explicit read-only identity probe. No send command."""
import argparse
import getpass
import json
from pathlib import Path

from companion_core.errors import AssistantError
from companion_core.storage import atomic_write

from .config import credential_key, load, validate
from .module import QQModule


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("configure", "check", "set-token", "probe"))
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--account")
    parser.add_argument("--friend", action="append", default=[])
    parser.add_argument("--group", action="append", default=[])
    parser.add_argument("--http", default="http://127.0.0.1:3000")
    parser.add_argument("--ws", default="ws://127.0.0.1:3001")
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            options = validate({"version": 1, "account": args.account, "private_targets": args.friend,
                                "groups": args.group, "http": args.http, "ws": args.ws})
            atomic_write(args.project / ".dev/qq/module.json", json.dumps(options, indent=2).encode())
        else:
            options = load(args.project)
            if options is None:
                raise AssistantError("qq_configuration", "请先生成QQ连接意向配置")
        if args.command == "set-token":
            import keyring

            secret = getpass.getpass("OneBot Token（隐藏输入，保存到系统凭证库）: ")
            if not 16 <= len(secret) <= 512:
                raise AssistantError("qq_token_required", "Token 长度不符合要求")
            keyring.set_password("local-companion-qq", credential_key(args.project, options["account"]), secret)
            secret = None
        result = {"configured": True, "client_accessed": False, "messages_sent": 0, "role_started": False}
        if args.command == "probe":
            from companion_service.config import open_store

            _config, store, _secret = open_store(args.project)
            try:
                result.update(QQModule(store, args.project, options=options).probe(), client_accessed=True)
            finally:
                store.database.close()
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({"error": getattr(exc, "code", "qq_setup_failed"), "messages_sent": 0, "role_started": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
