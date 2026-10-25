"""Browser-to-plugin handoff: stdin -> Windows DPAPI; no SDK, network or event loop."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import sys
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from .errors import AssistantError
from .storage import atomic_write

ORIGIN = "https://www.bilibili.com"
MAX_INPUT = 65536
FIELDS = ("sessdata", "bili_jct", "dedeuserid", "buvid3", "buvid4", "ac_time_value")
REQUIRED = FIELDS[:-1]
PENDING = ("awaiting_browser", "staged", "verifying")
TERMINAL = ("succeeded", "failed", "cancelled", "expired", "superseded", "uncertain")


def supported():
    return os.name == "nt"


def ticket_path(data, ticket_id, suffix=".json"):
    if not isinstance(ticket_id, str) or not re.fullmatch(r"[a-f0-9]{32}", ticket_id):
        raise AssistantError("invalid_login_ticket", "登录任务 ID 无效")
    root = Path(data).resolve()
    folder = (root / "runtime" / "browser-login").resolve()
    path = folder / (ticket_id + suffix)
    if not folder.is_relative_to(root) or path.is_symlink():
        raise AssistantError("invalid_login_path", "登录暂存目录不在本项目内")
    return path


def read_ticket(data, ticket_id):
    path = ticket_path(data, ticket_id)
    try:
        if path.stat().st_size > MAX_INPUT:
            raise ValueError()
        ticket = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(ticket, dict) or ticket["ticket_id"] != ticket_id:
            raise ValueError()
        return ticket
    except (OSError, ValueError, KeyError):
        raise AssistantError("login_ticket_missing", "登录任务不存在或记录损坏，请重新发起") from None


def write_ticket(data, ticket):
    path = ticket_path(data, ticket["ticket_id"])
    atomic_write(path, json.dumps(ticket, ensure_ascii=False).encode("utf-8"))


def expired(ticket, now=None):
    try:
        deadline = datetime.fromisoformat(ticket["expires_at"])
        return not deadline.tzinfo or (now or datetime.now(timezone.utc)) >= deadline
    except (ValueError, TypeError, KeyError):
        return True


def entropy(ticket):
    # Bind the encrypted handoff to this project, account, chat and attempt.
    context = {k: ticket[k] for k in (
        "ticket_id", "project_id", "owner", "expected_uid", "manifest_version", "expires_at",
    )}
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode("utf-8")).digest()


def crypt(raw, binding, *, decrypt=False):
    if not supported():
        raise AssistantError("browser_login_unsupported", "浏览器凭证导入当前仅支持 Windows 本地项目")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypto = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    source_buffer, entropy_buffer = ctypes.create_string_buffer(raw), ctypes.create_string_buffer(binding)
    source = Blob(len(raw), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    extra = Blob(len(binding), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    operation = crypto.CryptUnprotectData if decrypt else crypto.CryptProtectData
    operation.argtypes = [
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
    ]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, ctypes.byref(extra), None, None, 1, ctypes.byref(target)):
        raise AssistantError("login_encryption_failed", "Windows 用户绑定加密校验失败；未输出凭证")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def parse_secret(raw, ticket):
    try:
        if not 1 <= len(raw) <= MAX_INPUT:
            raise ValueError()
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict) or set(body) != {"origin", "credentials"} or body["origin"] != ORIGIN:
            raise ValueError()
        fields = body["credentials"]
        if (not isinstance(fields, dict) or set(fields) - set(FIELDS) or not set(REQUIRED) <= set(fields)
                or any(not isinstance(v, str) or not 1 <= len(v) <= 8192
                       or any(ord(c) < 33 or ord(c) == 127 or c == ";" for c in v) for v in fields.values())):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError):
        raise AssistantError("browser_credentials_invalid", "网页登录凭证不完整或格式无效，请重新登录官方网页") from None
    if fields["dedeuserid"] != ticket["expected_uid"]:
        raise AssistantError("login_account_mismatch", "浏览器登录账号与项目绑定账号不同；原凭证未变")
    return fields


def seal(data, ticket_id, raw):
    """Runs synchronously even inside the restricted browser tool's subprocess."""
    data = Path(data).resolve()
    with FileLock(str(data / "runtime" / "write.lock"), timeout=10):
        ticket = read_ticket(data, ticket_id)
        if expired(ticket):
            ticket_path(data, ticket_id, ".sealed").unlink(missing_ok=True)
            raise AssistantError("login_ticket_expired", "登录任务已超过 15 分钟，请重新发起")
        if ticket["status"] != "awaiting_browser":
            raise AssistantError("login_ticket_used", "此任务已暂存或结束，请查看状态，不重复提取")
        parse_secret(raw, ticket)
        path = ticket_path(data, ticket_id, ".sealed")
        encrypted = crypt(raw, entropy(ticket))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            ticket.update(status="staged", staged_at=datetime.now(timezone.utc).isoformat())
            write_ticket(data, ticket)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"ticket_id": ticket_id, "status": "staged", "encrypted": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--ticket", required=True)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(seal(args.data, args.ticket, sys.stdin.buffer.read(MAX_INPUT + 1))))
    except AssistantError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        raise SystemExit(2) from None
    except Exception:
        print('{"error":"login_handoff_failed","message":"凭证暂存失败；未输出敏感诊断"}')
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
