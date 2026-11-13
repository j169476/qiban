"""Exercise copied desktop builds with independent working and profile directories."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from companion_core.process_scope import ProcessScope


def manifest(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            if p.is_file() else None for p in sorted(directory.rglob("*"))}


def run_case(exe, profile, working):
    before = manifest(exe.parent)
    env = os.environ.copy()
    env["PATH"] = str(Path(env["SYSTEMROOT"]) / "System32")
    # Always use an explicit test profile: Windows Known Folders do not follow
    # a replaced LOCALAPPDATA environment variable.
    command = [str(exe), "--smoke", "--data-dir", os.path.relpath(profile, working)]
    scope = ProcessScope()
    try:
        process = subprocess.Popen(command, cwd=working, env=env, stdin=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
        scope.attach(process)
        scope.resume()
        assert process.wait(timeout=60) == 0, "Desktop smoke failed"
        result = json.loads((profile / "result.json").read_text(encoding="utf8"))
        assert result["passed"] and result["process_id"] == process.pid, "Missing fresh result"
        paths = next(e["detail"] for e in result["events"] if e["name"] == "app_paths")
        expected = {"root": profile, "application": exe.parent, "working_directory": working,
                    "data": profile / "data", "logs": profile / "logs",
                    "cache": profile / "cache", "temporary": profile / "temp",
                    "runtime_temp": profile / "temp", "resources": profile / "resources",
                    "webview": profile / "cache" / "webview"}
        for key, path in expected.items():
            assert Path(paths[key]) == path, (key, paths[key], str(path))
            assert path.is_dir(), (key, "not created")
        checks = [e["name"] for e in result["events"] if e["name"].startswith("check_")]
        assert len(checks) == 9
        events = [json.loads(line) for line in (profile / "logs" / "events.jsonl").read_text(encoding="utf8").splitlines()]
        assert events[-1]["name"] == "shutdown_completed"
        assert events[-1]["detail"] == {"state": "stopped", "detail": "graceful_stop"}
        assert not (profile / "events.jsonl").exists()
        assert not (profile / "webview").exists()
    finally:
        scope.close()
    assert manifest(exe.parent) == before, "Application directory changed during use"
    assert not list(working.iterdir()), "Working directory gained runtime files"
    return {"passed": True, "checks": checks, "application_unchanged": True,
            "working_directory_unchanged": True, "system_only_path": True,
            "application_exit_graceful": True,
            "paths": paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    source, evidence = args.exe.resolve(strict=True), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    working = evidence / "无关工作目录"
    working.mkdir()
    copies = [evidence / "程序 副本一", evidence / "更新后 副本二"]
    for directory in copies:
        shutil.copytree(source.parent, directory)
    profile = evidence / "独立 用户资料"
    report = {"passed": False, "cases": []}
    try:
        report["cases"].append(run_case(copies[0] / source.name, profile, working))
        sentinels = {profile / "data" / "合成资料.txt": b"synthetic user data",
                     profile / "resources" / "model-placeholder.txt": b"synthetic resource, not a model"}
        for path, content in sentinels.items():
            path.write_bytes(content)
        cache_marker = profile / "cache" / "old-cache-marker.txt"
        cache_marker.write_text("old", encoding="utf8")
        # Move only this fresh test's cache; retain the old bytes as evidence.
        old_cache = evidence / "保留的旧缓存"
        cache_source = (profile / "cache").resolve(strict=True)
        assert cache_source.is_relative_to(evidence) and old_cache.is_relative_to(evidence)
        cache_source.rename(old_cache)
        report["cases"].append(run_case(copies[1] / source.name, profile, working))
        for path, content in sentinels.items():
            assert path.read_bytes() == content, "Persistent data changed"
        assert not cache_marker.exists()
        assert (old_cache / cache_marker.name).read_text(encoding="utf8") == "old"
        report["data_and_resources_preserved"] = True
        report["cache_recreated"] = True
        fresh = evidence / "第二份独立资料"
        report["cases"].append(run_case(copies[1] / source.name, fresh, working))
        assert not list((fresh / "data").iterdir()) and not list((fresh / "resources").iterdir())
        report["fresh_profile_isolated"] = True
        report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        (evidence / "layout-result.json").write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                                     encoding="utf8")
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"]),
                      "report": str(evidence / "layout-result.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
