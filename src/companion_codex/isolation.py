"""Disposable, fail-closed Windows MXC boundary for the *entire* Codex process.

No platform or database credentials are included in the worker environment. The
trusted broker may read Codex's login, but supplies only a short-lived access token
through the app-server protocol, never a file, command argument or model input.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import time
import tomllib
from pathlib import Path

from companion_core.errors import AssistantError

DISABLED_FEATURES = (
    "shell_tool", "plugins", "apps", "computer_use", "browser_use", "in_app_browser",
    "multi_agent", "image_generation", "view_image", "hooks", "memories", "chronicle",
    "skill_search", "multi_agent_v2", "browser_use_external", "browser_use_full_cdp_access",
    "remote_plugin", "network_proxy",
    "goals", "sleep_tool", "workspace_dependencies", "worktrees", "in_app_local_automation",
    "skill_mcp_dependency_install", "auth_elicitation", "tool_suggest", "daemon_auto_start",
)
MODEL_SETTINGS = ("model", "model_reasoning_effort", "model_verbosity", "service_tier")
SERVICE_HOSTS = ("chatgpt.com", "api.openai.com", "auth.openai.com", "*.oaiusercontent.com")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()


def model_settings(home: Path | None = None) -> dict:
    try:
        settings = tomllib.loads(((home or codex_home()) / "config.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise AssistantError("model_config", "无法读取 Codex 模型设置") from None
    if settings.get("model_provider") not in (None, "openai"):
        raise AssistantError("model_config", "隔离工作进程当前仅支持 Codex 官方模型连接")
    return {k: settings[k] for k in MODEL_SETTINGS if isinstance(settings.get(k), str)}


def login_params(home: Path | None = None) -> dict:
    """Trusted broker only. Never log/return this through a tool or web endpoint."""
    try:
        path = (home or codex_home()) / "auth.json"
        if path.stat().st_size > 128_000:
            raise ValueError()
        auth = json.loads(path.read_text(encoding="utf-8"))
        if auth.get("auth_mode") != "chatgpt":
            raise ValueError()
        tokens = auth["tokens"]
        if not all(isinstance(tokens.get(k), str) and tokens[k] for k in ("access_token", "account_id")):
            raise ValueError()
        return {"type": "chatgptAuthTokens", "accessToken": tokens["access_token"],
                "chatgptAccountId": tokens["account_id"]}
    except (OSError, ValueError, KeyError, TypeError):
        raise AssistantError("model_auth", "无法取得 Codex 临时登录；请在 Codex 中重新登录后重试") from None


def _table(values: dict) -> str:
    return "{" + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in values.items()) + "}"


def _port() -> int:
    # If the port is claimed between reservation and startup the job fails closed.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class IsolatedJob:
    def __init__(self, parent: Path, *, native=None, settings=None, executable=None):
        if os.name != "nt":
            raise AssistantError("model_isolation", "尚未验收此系统的角色进程隔离，拒绝启动")
        if native not in (None, "web", "image"):
            raise AssistantError("native_kind", "不支持的原生工具任务")
        found = executable or shutil.which("codex")
        if not found or Path(found).suffix.lower() != ".exe":
            raise AssistantError("model_unavailable", "未找到原生 Codex 可执行文件")
        self.executable = Path(found).resolve(strict=True)
        parent.mkdir(parents=True, exist_ok=True)
        self.parent = parent.resolve()
        self.root = Path(tempfile.mkdtemp(prefix="isolated-", dir=self.parent)).resolve()
        self.home = self.root / "home"
        self.work = self.root / "work"
        self.temp = self.root / "temp"
        for folder in (self.home, self.work, self.temp):
            folder.mkdir()
        config = {
            "cli_auth_credentials_store": "ephemeral", "project_doc_max_bytes": 0,
            "web_search": "live" if native == "web" else "disabled",
            "approval_policy": "never", "default_permissions": ":read-only",
        }
        config.update({k: v for k, v in (settings or {}).items() if k in MODEL_SETTINGS and isinstance(v, str)})
        rows = [k + "=" + json.dumps(v) for k, v in config.items()]
        rows += ["[features]"]
        rows += [key + "=" + ("true" if key == "image_generation" and native == "image" else "false")
                 for key in DISABLED_FEATURES]
        (self.home / "config.toml").write_text("\n".join(rows) + "\n", encoding="utf-8")

    def environment(self) -> dict:
        # Allowlist, never strip secrets with a denylist. No API keys, Codex chat ID,
        # Python imports, Git hooks, user proxy credentials or database DSNs.
        source = {k.upper(): v for k, v in os.environ.items()}
        result = {k: source[k] for k in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC") if k in source}
        system = Path(result.get("SYSTEMROOT", r"C:\Windows"))
        result.update({
            "PATH": os.pathsep.join((str(self.executable.parent), str(system / "System32"), str(system))),
            "CODEX_HOME": str(self.home), "HOME": str(self.home), "USERPROFILE": str(self.home),
            "APPDATA": str(self.home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            "TEMP": str(self.temp), "TMP": str(self.temp),
        })
        return result

    def command(self, payload=None, *, extra_reads=(), network=True) -> list[str]:
        fs = {":root": "deny", ":minimal": "read", self.root.as_posix(): "write",
              self.executable.parent.as_posix(): "read"}
        fs.update({Path(p).resolve().as_posix(): "read" for p in extra_reads})
        command = [str(self.executable), "sandbox", "--include-managed-config", "-C", str(self.work),
                   "-P", "companion-isolated", "-c", 'windows.sandbox="mxc"',
                   "-c", 'default_permissions="companion-isolated"',
                   "-c", "permissions.companion-isolated.filesystem=" + _table(fs),
                   "-c", "permissions.companion-isolated.network.enabled=" + str(network).lower(),
                   "-c", "features.network_proxy=" + str(network).lower()]
        if network:
            command += ["-c", "permissions.companion-isolated.network.allow_local_binding=true",
                        "-c", "permissions.companion-isolated.network.enable_socks5=false",
                        "-c", "permissions.companion-isolated.network.allow_upstream_proxy=false",
                        "-c", 'permissions.companion-isolated.network.proxy_url="http://127.0.0.1:'
                        + str(_port()) + '"',
                        "-c", "permissions.companion-isolated.network.domains="
                        + _table({host: "allow" for host in SERVICE_HOSTS})]
        return command + ["--", *(payload or [str(self.executable), "app-server", "--listen", "stdio://"])]

    def close(self):
        # Delete only the exact fresh job we created; never follow a replacement
        # junction/symlink or accept a model-supplied cleanup path.
        if self.root.parent != self.parent or self.root.is_symlink() or self.root.resolve() != self.root:
            raise AssistantError("model_cleanup", "隔离工作目录边界发生变化")
        deadline = time.monotonic() + 3
        while self.root.exists():
            try:
                shutil.rmtree(self.root)
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise AssistantError("model_cleanup", "隔离进程退出后工作目录仍被占用") from None
                time.sleep(.05)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
