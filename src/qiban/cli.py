"""Retired legacy command entry.

The standalone Qiban application owns management now. This module stays
importable for the installed ``qiban`` console script, but it no longer starts
the old service, the browser management page, or the Codex management chat.
"""
import sys

NOTICE = ("旧的 qiban 命令行管理入口已停止使用：不再启动旧服务、网页管理页或 Codex 管理聊天。"
          "请运行新版 Qiban.exe，在应用内完成本机管理与运行。")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help", "--version"):
        print(NOTICE)
        return 0
    print(NOTICE, file=sys.stderr)
    raise SystemExit(2)
