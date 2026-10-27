"""Non-secret installation metadata plus a fail-closed operating-system vault."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from companion_core.errors import AssistantError
from companion_core.operation_context import stage
from companion_core.storage import atomic_write

VAULT = "companion-local-service"
FILENAME = ".companion-service.json"


def metadata(project):
    path = Path(project).resolve() / FILENAME
    if not path.exists():
        return None
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if (config["version"] != 1 or not re.fullmatch(r"[a-f0-9]{32}", config["id"])
                or Path(config["project"]).resolve() != Path(project).resolve()
                or config["database"]["host"] != "127.0.0.1"
                or type(config["database"]["port"]) is not int
                or not 1024 <= config["database"]["port"] <= 65535
                or set(config["database"]) - {"host", "port", "dbname", "user", "options"}
                or set(config["ports"]) != {"admin", "role"}
                or len(set(config["ports"].values())) != 2
                or any(type(p) is not int or not 1024 <= p <= 65535 for p in config["ports"].values())):
            raise ValueError()
        if not Path(config["data"]).resolve().is_relative_to(Path(project).resolve()):
            raise ValueError()
        return config
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        raise AssistantError("service_config", "服务配置无效；禁止回退至旧数据目录") from None


def secrets_for(config):
    import keyring

    try:
        raw = keyring.get_password(VAULT, config["id"])
        value = json.loads(raw) if raw else {}
        key = base64.b64decode(value["data_key"], validate=True)
        if len(key) != 32 or len(value["bridge_secret"]) < 32 or len(value["database_password"]) < 32:
            raise ValueError()
        return {**value, "key": key}
    except Exception:
        raise AssistantError("service_vault", "系统凭据库中的服务密钥不可用；未回退到明文") from None


def save(project, config, credentials):
    import keyring

    target = Path(project).resolve() / FILENAME
    if target.exists():
        raise AssistantError("service_exists", "已存在服务配置，不能覆盖")
    try:
        keyring.set_password(VAULT, config["id"], json.dumps(credentials))
    except Exception:
        raise AssistantError("service_vault", "无法保存系统凭据库") from None
    atomic_write(target, json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"))
    metadata(project)


def open_store(project):
    from bili_assistant.profiles import profile
    from companion_core.postgres import PostgresStore

    config = metadata(project)
    if config is None:
        raise AssistantError("service_missing", "尚未配置本机服务")
    from .managed_postgres import ensure

    with stage("database_start"):
        ensure(config)
    with stage("credential_store"):
        secret = secrets_for(config)
    with stage("database_connect"):
        store = PostgresStore(config["data"], {**config["database"], "password": secret["database_password"]},
                              secret["key"], profile())
    with stage("data_validation"):
        if not store.validate_critical()["ok"]:
            store.database.close()
            raise AssistantError("service_invalid", "服务资料校验未通过，保持暂停")
    return config, store, secret["bridge_secret"]


def urls(config):
    return {kind: "http://127.0.0.1:" + str(port) for kind, port in config["ports"].items()}
