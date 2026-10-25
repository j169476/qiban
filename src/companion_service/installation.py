"""Non-secret installation journal and a narrow, verified cutover recovery."""
import json
import os
import re
from pathlib import Path

from filelock import FileLock

from companion_core.errors import AssistantError
from companion_core.runtime_keys import RUN_KEYS
from companion_core.storage import atomic_write, utcnow

FILENAME = ".companion-install.json"
STAGES = ("reserved", "directories", "copied", "initialized", "database_started", "imported", "verified", "committed")


class Journal:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.path = self.project / FILENAME

    def read(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (data["version"] != 1 or data["stage"] not in STAGES
                    or not re.fullmatch(r"[a-f0-9]{32}", data["id"])
                    or Path(data["project"]).resolve() != self.project):
                raise ValueError()
            return data
        except (OSError, ValueError, KeyError, TypeError):
            raise AssistantError("installation_journal", "安装恢复记录无效，未更改原资料") from None

    def write(self, data):
        atomic_write(self.path, json.dumps({**data, "updated_at": utcnow()}, ensure_ascii=False, indent=2).encode())

    def begin(self, identifier, fingerprint):
        if self.read() is not None:
            raise AssistantError("installation_pending", "已有安装记录，请先查看 install-status，不能覆盖失败现场")
        self.write({"version": 1, "id": identifier, "project": str(self.project), "source_fingerprint": fingerprint,
                    "stage": "reserved", "error": None})

    def advance(self, stage, **metadata):
        if stage not in STAGES or set(metadata) - {"config", "target_fingerprint"}:
            raise ValueError("Invalid installation stage")
        data = self.read()
        if not data or STAGES.index(stage) < STAGES.index(data["stage"]):
            raise AssistantError("installation_journal", "安装阶段不可倒退")
        self.write({**data, **metadata, "stage": stage, "error": None})

    def failed(self, code):
        data = self.read()
        if data:
            self.write({**data, "error": code if re.fullmatch(r"[a-z_]{1,80}", str(code)) else "installation_interrupted"})

    def status(self):
        data = self.read()
        if data is None:
            return {"pending": False, "role_started": False}
        return {key: data[key] for key in ("id", "stage", "error", "updated_at")} | {
            "can_finalize": data["stage"] == "verified", "role_started": False,
            "recovery": "install-finalize" if data["stage"] == "verified" else "retain_for_review"}


def finalize(project):
    """Resume only after the original import and restricted-role check succeeded.

    Earlier stages retain their exact vault ID and files for inspection. They are
    never deleted, re-imported over, or silently redirected into another cluster.
    """
    from bili_assistant.profiles import profile
    from companion_core.legacy_import import capture
    from companion_core.postgres import PostgresStore
    from companion_core.postgres_transfer import fingerprint

    from .config import metadata, save, secrets_for
    from .managed_postgres import ensure

    journal = Journal(project)
    with FileLock(str(journal.project / ".companion-install.lock"), timeout=0):
        data = journal.read()
        if not data or data["stage"] != "verified":
            raise AssistantError("installation_stage", "只有已完成数据库验证的安装可以继续切换")
        config = data.get("config", {})
        expected_runtime = Path(os.environ["LOCALAPPDATA"]) / "Companion" / data["id"]
        if (config.get("id") != data["id"] or config.get("project") != str(journal.project)
                or Path(config.get("data", "")).resolve() != journal.project / "companion-service-data"
                or Path(config.get("postgres_runtime", "")).resolve() != expected_runtime.resolve()
                or config.get("database", {}).get("host") != "127.0.0.1"):
            raise AssistantError("installation_journal", "恢复记录不属于此安装的受限目录")
        current = metadata(project)
        if current is not None and current != config:
            raise AssistantError("installation_conflict", "已有其他服务配置，不能覆盖")
        source = journal.project / "bili-assistant-data"
        if data["source_fingerprint"] is not None:
            if capture(source, profile())["fingerprint"] != data["source_fingerprint"]:
                raise AssistantError("source_changed", "旧资料在中断后发生变化，不能使用过时导入继续切换")
        elif source.exists():
            raise AssistantError("recovery_destination", "灾难恢复目标出现了旧资料，不能继续切换")
        secret = secrets_for(config)
        ensure(config)
        store = PostgresStore(config["data"], {**config["database"], "password": secret["database_password"]}, secret["key"], profile())
        try:
            with FileLock(str(store.runtime / "service.lock"), timeout=0), store.lock:
                if (not store.validate_critical()["ok"]
                        or any(store.state(key, {}).get("status") == "active" for key in RUN_KEYS)
                        or fingerprint(store) != data.get("target_fingerprint")):
                    raise AssistantError("installation_verification", "数据库未通过暂停状态及原验证快照复核")
                if current is None:
                    save(project, config, {key: value for key, value in secret.items() if key != "key"})
                if data["source_fingerprint"] is not None:
                    atomic_write(source / ".service-retired.json", json.dumps({"service": data["id"],
                        "source_fingerprint": data["source_fingerprint"]}).encode())
                journal.advance("committed")
                return {"installed": True, "recovered_cutover": True, "role_started": False}
        finally:
            store.database.close()
