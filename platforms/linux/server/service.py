"""Single background worker and v1 read-only management socket."""
import asyncio
import fcntl
import json
import os
import re
import signal
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import jobs

SOCKET = Path("/run/qiban/status.sock")
LIMIT = 65536
PLATFORMS = {
    key: {"available": False, "status": "unavailable", "reason": reason} for key, reason in {
        "local": "会客室尚未接入，本机仅运行文档索引任务。",
        "bilibili": "本轮尚未接入 B 站账号。",
        "qq": "本轮尚未接入 QQ。",
        "wechat": "本轮尚未接入微信。",
    }.items()
}


def error(identifier, code, message):
    return {"request_id": identifier, "ok": False, "error": code, "message": message}


class Service:
    def __init__(self, db):
        self.db = db
        self.installation = jobs.initialize(db)
        self.instance = uuid.uuid4().hex
        self.heartbeat = 0

    def dispatch(self, req, negotiated):
        identifier = req.get("request_id") if isinstance(req, dict) else None
        if not isinstance(identifier, str) or not re.fullmatch("[0-9a-f]{32}", identifier):
            return error(None, "invalid_request", "请求标识无效。"), negotiated
        if (set(req) != {"action", "operation", "arguments", "request_id"} or req["action"] != "management"
                or not isinstance(req["arguments"], dict)):
            return error(identifier, "invalid_request", "管理请求格式无效。"), negotiated
        op, args = req["operation"], req["arguments"]
        common = {"protocol_version": 1, "installation_id": self.installation, "runtime_instance": self.instance}
        if op == "protocol.hello":
            versions = args.get("versions")
            if (set(args) != {"versions"} or not isinstance(versions, list) or not 1 <= len(versions) <= 32
                    or any(type(v) is not int for v in versions) or 1 not in versions):
                return error(identifier, "protocol_incompatible", "没有共同支持的协议版本。"), False
            return {"request_id": identifier, "ok": True, "result": {**common, "operations": ["runtime.status"]}}, True
        if not negotiated:
            return error(identifier, "handshake_required", "请先协商协议版本。"), False
        if op != "runtime.status":
            return error(identifier, "operation_not_allowed", "此连接仅提供状态读取。"), negotiated
        if args:
            return error(identifier, "invalid_request", "状态读取不接受参数。"), negotiated
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        result = {**common, "generated_at": stamp, "status": "running", "heartbeat_count": self.heartbeat,
                  "progress": jobs.progress(self.db), "platforms": PLATFORMS}
        return {"request_id": identifier, "ok": True, "result": result}, negotiated

    async def client(self, reader, writer):
        negotiated = False
        try:
            while line := await reader.readline():
                if len(line) > LIMIT or not line.endswith(b"\n"):
                    break
                try:
                    req = json.loads(line.decode("utf-8"))
                    reply, negotiated = self.dispatch(req, negotiated)
                except (ValueError, RecursionError):
                    reply = error(None, "invalid_request", "请求不是有效的 UTF-8 JSON。")
                writer.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                await writer.drain()
        except (ValueError, ConnectionError):
            pass
        finally:
            writer.close()

    async def run(self):
        stopping = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stopping.set)
        SOCKET.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self.client, path=SOCKET, limit=LIMIT)
        os.chmod(SOCKET, 0o660)
        async with server:
            while not stopping.is_set():
                jobs.work_one(self.db)
                self.heartbeat += 1
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=0.5)
                except TimeoutError:
                    pass


if __name__ == "__main__":
    with open("/var/lib/qiban/worker.lock", "a") as lock, closing(jobs.connect()) as database:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(Service(database).run())
