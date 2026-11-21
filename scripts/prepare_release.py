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
    "output/design-art/room-static-v1/readme-room.webp": "0d9344e812d0579b5a2c4d9ebb320d377ac0fd735a6281e1a3ccaeae6803871e",
    "src/companion_core/history_model/model_quantized.onnx": "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc",
    "src/companion_core/history_model/tokenizer.json": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    "scripts/desktop_probe/Assets/hoodie-avatar.webp": "3204a6770956ff4da4d39920a4c6b9d0b67614ebf94d75d42a747c2528bcfb23",

    "scripts/desktop_probe/Assets/qiban.ico": "3a1a57b8a4f68766af8361ef6c0909bee15cb1d2fc1d7e2681d08eeaa1cb4f2f",
    "scripts/desktop_probe/licenses/Windows-SDK.rtf": "dd07eb178e00c6bba4148457fc00ff77cd4887eb521d504186fe59c9ec8bbe62",
    # Upstream source archives, pinned by the same SHA-256 values as build_storage.py.
    "scripts/desktop_probe/upstream/bilibili_api_python-17.4.2.tar.gz": "3ec62eaa45702779957eaf70fba0c5ff7c858054804cb605780f76fee388ebd5",
    "scripts/desktop_probe/upstream/qrcode-terminal-0.8.tar.gz": "1e2b69e662b9346e98dd95983033e9d43cff0643d8afda12605f515428e666c0",
    "scripts/desktop_probe/Assets/white-dragon-tea.png": "d0a92800b16bcc0d2048c1f9efe54a619270047f3ed91d96fd380da11fc5ca98",
}
ROOM_ORIGINALS = "output/design-art/room-static-v1"
ROOM_IMAGES = {
    "backgrounds/midnight": "63d5fc0d8f9240b74e96a1d66b748ae2484d977e7737312cf7310d392b5da6c5",
    "backgrounds/predawn": "4a54df6dc851faeaea38d0e4298346de0f9ec2fe1e399e1df6db35800d8a7129",
    "backgrounds/morning": "108086b5e73e0c90b57677c595af7d2c57f7175f1bfb44c7802dd3e36de68ef3",
    "backgrounds/forenoon": "7283549d718f09fef5c1c1a3558613d667309ebfdb40056ffbeeed3b989ba9e9",
    "backgrounds/noon": "2f3239ffb9f7e9c006cc756c3c15174c77841912d5e08cdae277e5a9dcb113b0",
    "backgrounds/afternoon": "f9fe940a10afaa2eaaa399a04a17556f1554dbc5427a4dd0bedb1804132a0800",
    "backgrounds/dusk": "19fa62d95f7a74b3a5aa307d4479b24af18f138cb0695afab8c35ef8d43e9998",
    "backgrounds/night": "4939b41779c68488b732e51bf23f405e5ac323e038e12ac45f020481d58176ff",
    "seasons/spring": "875006b6fae633cd519bce6e3dd54fe3e4f709feaad94a1fdcadbc771ec1f5a5",
    "seasons/summer": "3cd17c54258c4ca9f991376703b1928c8165576f08c611fa9b47e331d025694e",
    "seasons/autumn": "0b0f02c87ebfbb9f1e487960586b77e5886540d8b3b8bf4155116b7509b19b84",
    "seasons/winter": "c8556bba4a039a54c24d7ec160eca5efa43308660c08816d26a391610397e475",
    "characters/hoodie/replying": "595eee8f84a31a9023961c56385391d4d80a17ae274c094a1bb8d30f16cc7060",
    "characters/hoodie/learning": "4c0c9457d1f082430b0ad245daeddcbc0e860ce7eb560dde1f1cacc9c1060daa",
    "characters/hoodie/watching": "90c1066e5cb8c1585cf27545bafcc9ce0b399dd148198c396149bcd0e097df2e",
    "characters/hoodie/sleeping": "2fcd590d6f9b30251ae0389f6ce915ea96918bb4a3d137627faf49b0270dd732",
    "characters/hoodie/reading": "a58739ae8ed1f9198747ed6a3d1b0d3cbee67c45a74ea6eed39b32b3cac7adca",
    "characters/hoodie/window": "a616288ce879ec1f5cb1629f56c8fbba0321d24135089f80cf25232ee7d54721",
    "characters/hoodie/tail": "2435ffd853f954a698ef6178821757acba0dd92137d0668cd93ef80d415c7576",
}
for original, digest in ROOM_IMAGES.items():
    name = original.rsplit("/", 1)[-1]
    published = name + ("-still" if original.startswith("characters/") else "")
    REVIEWED_ASSETS[f"{ROOM_ORIGINALS}/{original}.webp"] = digest
    REVIEWED_ASSETS[f"scripts/desktop_probe/Assets/room-stills/{published}.webp"] = digest

# Confirmed public text outside EXTENSIONS; each exact path is still text-scanned.
REVIEWED_TEXT = {
    "scripts/desktop_probe/Assets/dragon-rest.svg",
    "scripts/desktop_probe/installer/Qiban.iss",
    "src/companion_qq/native/LICENSE.Waylay",
    "src/companion_qq/native/QQNT-lifecycle.def",
    "src/companion_qq/native/qqnt-lifecycle.c",
}

ROOT_FILES = {
    "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "pyproject.toml", ".gitignore", ".gitattributes",
    "constraints-windows.txt", ".agents/plugins/marketplace.json",
    "output/design-review/tools/receive_room_art.py", "output/design-review/tools/check_pixel_room.py",
    "docs/DEVELOPMENT.md", "docs/GETTING_STARTED.md", "docs/RELEASE_NOTES.md",
    "docs/微信支持范围.md", "docs/桌面声明.md", "docs/模型选择参考.md",
    "platforms/linux/.gitkeep",
}
SOURCE_DIRS = {"src", "tests", "tests_core", "tests_postgres", "tests_wechat", "scripts",
               "plugins", "packages", "requirements", ".github", ROOM_ORIGINALS}
EXTENSIONS = {".py", ".pyi", ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".sql",
              ".js", ".mjs", ".cjs", ".html", ".css", ".cmd", ".ps1", ".cpp", ".h", ".inc", ".helper", ".cs", ".csproj"}
SKIP_DIRS = {".git", ".dev", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"}
# Locally generated desktop build output; regenerated by the build and never shipped.
LOCAL_BUILD_PREFIXES = ("scripts/desktop_probe/bin/", "scripts/desktop_probe/obj/")
# Personal material and internal acceptance records (docs/QIBAN_*) stay local.
PRIVATE_PREFIXES = ("docs/evidence/", "docs/personas/", "docs/design/", "assets/avatars/",
                    "docs/V8.2-", "docs/V0_9_DEVELOPMENT.md", "docs/QIBAN_")
SKIP_FILES = {"runtime.json", "local-settings.json"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".pem", ".key",
                 ".ccbackup", ".cckey", ".zip", ".whl"}
# Explicitly approved public attribution and repository; other identifiers stay blocked.
PUBLIC_TEXT = ("Yi_Ning和ta的龙娘", "https://github.com/j169476/qiban", "github.com/j169476/qiban", "j169476/qiban")
PUBLIC_TEXT_FILES = {
    "README.md", "docs/桌面声明.md",
    "scripts/prepare_release.py", "tests/test_release_snapshot.py",
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
        if not base.is_dir() or any(linked(p) for p in (base, *base.parents) if p != root and p.is_relative_to(root)):
            raise ReleaseError(f"Missing or linked source directory: {name}")
        for directory, dirs, files in os.walk(base, followlinks=False):
            parent = Path(directory)
            for child in list(dirs):
                path = parent / child
                relative = path.relative_to(root).as_posix() + "/"
                if (child in SKIP_DIRS or child.endswith(".egg-info") or relative.startswith(PRIVATE_PREFIXES)
                        or relative.startswith(LOCAL_BUILD_PREFIXES)):
                    dirs.remove(child)
                elif linked(path):
                    raise ReleaseError(f"Linked source directory: {relative}")
            for child in sorted(files):
                path = parent / child
                relative = path.relative_to(root).as_posix()
                lower = child.lower()
                if (relative.startswith(PRIVATE_PREFIXES) or relative.startswith(LOCAL_BUILD_PREFIXES)
                        or child in SKIP_FILES or path.suffix.lower() in SKIP_SUFFIXES or lower.startswith(".env")
                        or (lower.endswith(".json") and ("cookies" in lower or "credentials" in lower))):
                    continue
                if linked(path) or (path.suffix.lower() not in EXTENSIONS and child != "LICENSE"
                                    and relative not in REVIEWED_ASSETS and relative not in REVIEWED_TEXT):
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
        limit = 24_010_842 if relative == "src/companion_core/history_model/model_quantized.onnx" else 10_000_000
        if path.stat().st_size > limit:
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
