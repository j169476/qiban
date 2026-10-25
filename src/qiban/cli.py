"""Qiban public command; existing bili-assistant commands remain compatible."""
import argparse
import json
import sys
from pathlib import Path

from companion_core.errors import AssistantError


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "restart":
        from bili_assistant.cli import main as legacy_main

        return legacy_main()
    parser = argparse.ArgumentParser(description="Explicitly reconnect Qiban to the running Codex desktop")
    parser.add_argument("command", choices=["restart"])
    parser.add_argument("--project", type=Path, default=Path.cwd())
    args = parser.parse_args()
    from companion_service.bridge import call
    from companion_service.config import open_store

    try:
        _, store, _ = open_store(args.project)
        try:
            thread = store.state("service-admin-thread")
        finally:
            store.database.close()
        if not thread:
            raise AssistantError("admin_required", "请先在 Codex 管理聊天中调用 @qiban 绑定管理入口")
        value = call(args.project, "service_resume", {}, thread=thread)
        print(json.dumps(value, ensure_ascii=False))
    except AssistantError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        raise SystemExit(2) from None
