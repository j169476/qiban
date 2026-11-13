"""Private UTF-8 pipe entry point for the bundled Windows WeChat reader."""
import sys

from companion_wechat.db_worker import main

if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
