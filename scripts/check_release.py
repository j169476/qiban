"""Check source versions and, optionally, the installed plugin without starting it."""

import argparse
import hashlib
import json
import tomllib
from pathlib import Path

from bili_assistant import __version__
from companion_codex import __version__ as codex_version
from companion_core import __version__ as core_version


def inspect(project, installed=None):
    plugin = project / "plugins" / "bili-assistant"
    versions = {"python": __version__, "core": core_version, "codex_bridge": codex_version,
                "core_package": tomllib.loads((project / "packages/companion-core/pyproject.toml").read_text(
                    encoding="utf-8"))["project"]["version"], "package": tomllib.loads(
        (project / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]}
    for name in ("plugin.json", ".codex-plugin/plugin.json"):
        versions[name] = json.loads((plugin / name).read_text(encoding="utf-8"))["version"]
    mismatches = []
    if installed is not None:
        for relative in ("plugin.json", ".codex-plugin/plugin.json", ".mcp.json", "mcp.json",
                         "scripts/run.py", "scripts/browser-login.mjs", "hooks/lifecycle.py", "hooks/hooks.json",
                         "skills/bili-start/SKILL.md", "skills/bili-start/agents/openai.yaml",
                          "skills/wechat-start/SKILL.md", "skills/wechat-start/agents/openai.yaml",
                          "skills/qq-start/SKILL.md", "skills/qq-start/agents/openai.yaml",
                          "skills/local-start/SKILL.md", "skills/local-start/agents/openai.yaml",
                         "skills/bili-admin/SKILL.md", "skills/bili-admin/agents/openai.yaml",
                         "skills/bili-start/references/content-workflows.md",
                          "skills/bili-start/references/persona-and-memory.md",
                          "skills/bili-admin/references/browser-login.md",
                           "skills/bili-admin/references/background-tools.md",
                            "skills/bili-admin/references/storage-recovery.md",
                             "skills/bili-admin/references/core-identity.md",
                             "skills/bili-admin/references/runtime-metrics.md"):
            source, target = plugin / relative, installed / relative
            if not target.is_file() or hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(
                target.read_bytes()
            ).digest():
                mismatches.append(relative)
        source_runtime, target_runtime = plugin / "runtime.json", installed / "runtime.json"
        if source_runtime.exists() and (
            not target_runtime.exists() or json.loads(source_runtime.read_text(encoding="utf-8")) !=
            json.loads(target_runtime.read_text(encoding="utf-8"))
        ):
            mismatches.append("runtime.json")
    return {"ok": len(set(versions.values())) == 1 and not mismatches, "versions": versions,
            "installed_checked": installed is not None, "mismatches": mismatches}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", type=Path)
    args = parser.parse_args()
    result = inspect(Path(__file__).resolve().parents[1], args.installed)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
