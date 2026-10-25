"""Prepare a local installation without editing user settings or trusting hooks."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--register", action="store_true", help="写入本项目插件目录清单，不自动信任钩子")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    runtime = project / ".venv"
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(runtime)], check=True)
    install = [str(python), "-m", "pip", "install"]
    constraints = project / "constraints-windows.txt"
    if os.name == "nt" and constraints.exists():
        install += ["-c", str(constraints)]
    subprocess.run(install + ["-e", f"{project}[dev,service]"], check=True)
    data = project / "bili-assistant-data"
    plugin = project / "plugins" / "qiban"
    settings = {"project": str(project), "python": str(python), "data": str(data)}
    (plugin / "runtime.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    from companion_service.config import metadata

    if metadata(project) is None:
        subprocess.run([str(python), "-m", "qiban", "--project", str(project), "init"], check=True)
    if args.register:
        path = project / ".agents" / "plugins" / "marketplace.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        marketplace = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"name": "xiaolongniang-local", "plugins": []}
        )
        entry = {
            "name": "qiban",
            "source": {"source": "local", "path": "./plugins/qiban"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        }
        # Migrate only this project's known old entry; preserve unrelated plugins.
        for old in list(marketplace["plugins"]):
            if (old["name"] == "bili-assistant" and old.get("source") == {
                    "source": "local", "path": "./plugins/bili-assistant"}):
                marketplace["plugins"].remove(old)
        existing = next((p for p in marketplace["plugins"] if p["name"] == "qiban"), None)
        if existing and existing != entry:
            raise SystemExit("已有同名不同配置的插件条目，请手工检查；未覆盖。")
        if not existing:
            marketplace["plugins"].append(entry)
        path.write_text(json.dumps(marketplace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("本地环境就绪。请在 Codex 插件目录安装 栖伴 Qiban并审阅/信任生命周期钩子。")


if __name__ == "__main__":
    main()
