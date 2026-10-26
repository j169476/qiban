"""Audit/export a reviewed source and UI asset snapshot without Git history or local data.

This is a bounded release check, not a general secret detector. Review the file
manifest and use --deny-file for private identifiers known to the maintainer.
Never print matching text, credential values or the deny list.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import zipfile
from pathlib import Path

import yaml

REVIEWED_ASSETS = {
    "src/companion_service/static/white-dragon-tea.png": "d0a92800b16bcc0d2048c1f9efe54a619270047f3ed91d96fd380da11fc5ca98",
    "src/companion_service/static/icons.svg": "76a6440f6721b32452f10265d08a0ba6c3113178ec583c0ddfa355793247a20c"
}

ROOT_FILES = {
    "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "pyproject.toml", ".gitignore", ".gitattributes",
    "constraints-windows.txt", ".agents/plugins/marketplace.json",
}
SOURCE_DIRS = {"src", "tests", "tests_core", "tests_postgres", "tests_wechat", "scripts",
               "plugins", "packages", "requirements", "docs", ".github"}
EXTENSIONS = {".py", ".pyi", ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".sql",
              ".js", ".mjs", ".html", ".css", ".cmd", ".ps1", ".cpp", ".h", ".inc", ".helper"}
SKIP_DIRS = {".git", ".dev", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"}
PRIVATE_PREFIXES = ("docs/evidence/", "docs/personas/", "assets/avatars/",
                    "docs/V8.2-", "docs/V0_9_DEVELOPMENT.md")
SKIP_FILES = {"runtime.json", "local-settings.json"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".pem", ".key",
                 ".ccbackup", ".cckey", ".zip", ".whl"}
# Explicitly approved public attribution and repository; other identifiers stay blocked.
PUBLIC_TEXT = ("YiNing和ta的小龙", "https://github.com/j169476/qiban", "github.com/j169476/qiban", "j169476/qiban")
PUBLIC_TEXT_FILES = {
    "README.md", "docs/ABOUT.md", "src/companion_service/templates/about.html",
    "tests/qiban_ui.mjs", "scripts/prepare_release.py", "tests/test_release_snapshot.py",
}

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b"),
)


class ReleaseError(ValueError):
    pass


def linked(path):
    value = path.lstat()
    return path.is_symlink() or bool(getattr(value, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def source_paths(root):
    """Only explicitly selected source areas; never traverse links/junctions."""
    root = root.resolve(strict=True)
    for relative in sorted(ROOT_FILES):
        path = root / relative
        if not path.is_file():
            raise ReleaseError(f"Missing required source: {relative}")
        if any(linked(p) for p in (path, *path.parents) if p != root and p.is_relative_to(root)):
            raise ReleaseError(f"Linked source: {relative}")
        yield path
    for name in sorted(SOURCE_DIRS):
        base = root / name
        if not base.is_dir() or linked(base):
            raise ReleaseError(f"Missing or linked source directory: {name}")
        for directory, dirs, files in os.walk(base, followlinks=False):
            parent = Path(directory)
            for child in list(dirs):
                path = parent / child
                relative = path.relative_to(root).as_posix() + "/"
                if child in SKIP_DIRS or child.endswith(".egg-info") or relative.startswith(PRIVATE_PREFIXES):
                    dirs.remove(child)
                elif linked(path):
                    raise ReleaseError(f"Linked source directory: {relative}")
            for child in sorted(files):
                path = parent / child
                relative = path.relative_to(root).as_posix()
                lower = child.lower()
                if (relative.startswith(PRIVATE_PREFIXES) or child in SKIP_FILES
                        or path.suffix.lower() in SKIP_SUFFIXES or lower.startswith(".env")
                        or (lower.endswith(".json") and ("cookies" in lower or "credentials" in lower))):
                    continue
                if linked(path) or (path.suffix.lower() not in EXTENSIONS and child != "LICENSE" and relative not in REVIEWED_ASSETS):
                    raise ReleaseError(f"Unreviewed source file: {relative}")
                yield path


def blank_persona(data):
    return (data.get("name") == "" and data.get("core") == {"description": "", "speaking_style": []}
            and data.get("interests") == [] and data.get("foundation_memories") == []
            and "visual_identity" not in data and "relationship_baseline" not in data)


def audit(root, deny=()):
    root = Path(root).resolve(strict=True)
    payload = {}
    for path in source_paths(root):
        relative = path.relative_to(root).as_posix()
        if path.stat().st_size > 10_000_000:
            raise ReleaseError(f"Oversized source file: {relative}")
        if relative in REVIEWED_ASSETS:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != REVIEWED_ASSETS[relative]:
                raise ReleaseError(f"Reviewed asset changed: {relative}")
            payload[relative] = raw
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            raise ReleaseError(f"Non-text source file: {relative}") from exc
        if "\x00" in text:
            raise ReleaseError(f"Binary source file: {relative}")
        private_text = text
        if relative in PUBLIC_TEXT_FILES:
            for approved in PUBLIC_TEXT:
                private_text = private_text.replace(approved, "")
        if any(marker in private_text for marker in deny) or any(pattern.search(text) for pattern in SECRET_PATTERNS):
            raise ReleaseError(f"Private marker or credential pattern: {relative}")
        payload[relative] = text.encode("utf-8")
    persona = yaml.safe_load(payload["src/bili_assistant/defaults/persona.yaml"])
    core = json.loads(payload["src/companion_core/defaults.json"])
    if not blank_persona(persona) or not blank_persona(core["persona"]):
        raise ReleaseError("Release persona must be empty, with no foundation or visual/relationship settings")
    manifest = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in sorted(payload.items())]
    return payload, {"ok": True, "files": len(manifest), "bytes": sum(item["bytes"] for item in manifest),
                     "blank_persona": True, "history_included": False, "manifest": manifest}


def export(root, output, deny=()):
    root, output = Path(root).resolve(strict=True), Path(output).resolve()
    if output == root or root.is_relative_to(output):
        raise ReleaseError("Output cannot contain or replace the source repository")
    if output.is_relative_to(root) and not output.is_relative_to(root / ".dev/releases"):
        raise ReleaseError("In-repository exports must be under .dev/releases")
    if output.exists():
        raise ReleaseError("Output exists; choose a new directory, never overwrite a reviewed release")
    payload, report = audit(root, deny)
    output.mkdir(parents=True)
    tree = output / "repository"
    tree.mkdir()
    for relative, data in sorted(payload.items()):
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    archive = output / "source.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for relative, data in sorted(payload.items()):
            item = zipfile.ZipInfo("repository/" + relative, date_time=(2026, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            bundle.writestr(item, data)
    report["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, help="New output directory; omit for read-only audit")
    parser.add_argument("--deny-file", type=Path, help="Local UTF-8 identifiers, one per line; never exported")
    args = parser.parse_args()
    deny = tuple(line.strip() for line in args.deny_file.read_text(encoding="utf-8-sig").splitlines()
                 if line.strip()) if args.deny_file else ()
    try:
        if args.output:
            report = export(args.project, args.output, deny)
        else:
            _, report = audit(args.project, deny)
    except ReleaseError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({k: v for k, v in report.items() if k != "manifest"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
