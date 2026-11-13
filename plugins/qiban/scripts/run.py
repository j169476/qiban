"""Retired Codex plugin launcher.

The MCP entry used to start the old management service through the project's
isolated runtime. The standalone Qiban application owns management now, so this
launcher only reports the retirement: it starts no service, opens no web page,
binds no management chat, reads no runtime.json and guesses no executable path.
"""
import sys

NOTICE = ("栖伴的 Codex 插件管理入口已停止使用：不再启动旧服务、网页管理页或绑定管理聊天。"
          "请运行新版 Qiban.exe，在应用内查看“应用使用说明”了解现行流程。")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help", "--version"):
        print(NOTICE)
        return 0
    print(NOTICE, file=sys.stderr)
    raise SystemExit(2)


main()
