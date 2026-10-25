"""Offline module setup and artifact checks. Never connects to WeChat."""
import argparse
import hashlib
import json
from pathlib import Path

from .contracts import require
from .module import load_options
from .native.bridge import validate_build
from .native.protocol import build_signature, pe_exports


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["configure", "check", "manifest"])
    parser.add_argument("--project", type=Path, default=Path.cwd())
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--family", action="store_true", help="已停用：请在管理网页或管理聊天设置会话回复授权")
    group.add_argument("--no-family", action="store_true", help="已停用：请在管理网页或管理聊天设置会话回复授权")
    args = parser.parse_args(argv)
    project = args.project.resolve()
    dll = project / ".dev/wechat/native-module-build/xln_native_module.dll"
    try:
        require(not args.family and not args.no_family, "reply_authorization_management_required")
        if args.command == "manifest":
            data = dll.read_bytes()
            pe_exports(data)
            dll.with_suffix(".json").write_text(json.dumps({"sources": build_signature(),
                "sha256": hashlib.sha256(data).hexdigest()}, indent=2) + "\n", encoding="utf-8")
        elif args.command == "configure":
            path = project / ".dev/wechat/module.json"
            old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            value = old or {"version": 2,
                "locations": ".dev/wechat/database-locations.json", "targets": ".dev/wechat/test-targets-intent.json",
                "database_python": ".dev/wechat-db-venv/Scripts/python.exe",
                "native_dll": ".dev/wechat/native-module-build/xln_native_module.dll"}
            value.pop("family_enabled", None)
            value["version"] = 2
            validate_build(project / value["native_dll"])
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".pending")
            temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        options = load_options(project)
        if args.command != "manifest":
            require(options is not None, "wechat_module_configuration")
            for name in ("locations", "targets", "database_python", "native_dll"):
                require(options[name].is_file(), "wechat_module_file_missing")
            validate_build(options["native_dll"])
        print(json.dumps({"status": "offline_ready", "reply_authorization_required": True, "default_allow": False,
                          "client_accessed": False, "started": False, "messages_sent": 0}))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", "wechat_module_artifact_invalid")
        print(json.dumps({"status": "not_ready", "code": code, "client_accessed": False, "messages_sent": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
