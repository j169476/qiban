"""Build an isolated onedir storage backend; no account or database is bundled."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def copy_licenses(bundle, lock):
    licenses = bundle / "licenses"
    licenses.mkdir()
    packages = []
    # Retain all build environment license texts, including native-wheel third-party notices.
    # This list is deliberately a superset; it is not a runtime-module inventory.
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        copied = []
        for file in dist.files or []:
            if any(word in file.name.lower() for word in ("license", "licence", "copying", "notice")) and dist.locate_file(file).is_file():
                target = licenses / name / str(file).replace("../", "_parent_/")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(dist.locate_file(file), target)
                copied.append(target.relative_to(bundle).as_posix())
        if name.lower()=="qrcode-terminal" and not copied:
            # Version 0.8 declares MIT in PKG-INFO but ships no LICENSE file.
            target = licenses / name / "METADATA"
            target.parent.mkdir(exist_ok=True)
            target.write_text(dist.read_text("METADATA"),encoding="utf8")
            copied.append(target.relative_to(bundle).as_posix())
        if not copied:
            raise RuntimeError(f"Missing license text for {name}")
        packages.append({"name": name, "version": dist.version, "license_files": copied})
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", licenses / "CPython-LICENSE.txt")
    shutil.copyfile(ROOT / "LICENSE", licenses / "Qiban-LICENSE.txt")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", licenses / "Qiban-THIRD_PARTY_NOTICES.md")
    shutil.copyfile(lock, bundle / lock.name)
    (bundle / "build-packages.json").write_text(json.dumps(packages, indent=2), encoding="utf8")
    sources = bundle / "upstream-source"
    sources.mkdir()
    for filename, expected in {
        "bilibili_api_python-17.4.2.tar.gz":"3ec62eaa45702779957eaf70fba0c5ff7c858054804cb605780f76fee388ebd5",
        "qrcode-terminal-0.8.tar.gz":"1e2b69e662b9346e98dd95983033e9d43cff0643d8afda12605f515428e666c0",
    }.items():
        source = Path(__file__).with_name("upstream") / filename
        if hashlib.sha256(source.read_bytes()).hexdigest()!=expected:
            raise RuntimeError("Upstream source checksum mismatch: "+filename)
        shutil.copyfile(source,sources/filename)
    with zipfile.ZipFile(sources/"qiban-sources.zip","w",compression=zipfile.ZIP_DEFLATED) as archive:
        paths = [ROOT/name for name in ("LICENSE","THIRD_PARTY_NOTICES.md","pyproject.toml")]
        for package in ("bili_assistant","companion_core","companion_codex","companion_service",
                        "companion_qq","companion_wechat","qiban"):
            paths.extend((ROOT/"src"/package).rglob("*"))
        probe = Path(__file__).parent
        paths.extend(probe.glob("*"))
        for folder in ("Assets","licenses","upgrade"):
            paths.extend((probe/folder).rglob("*"))
        for path in sorted(paths):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc",".pyo"):
                archive.write(path,path.relative_to(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    output, work = args.output.resolve(), args.work.resolve()
    output.mkdir(parents=True, exist_ok=False)
    work.mkdir(parents=True, exist_ok=False)
    # Build in a dedicated venv installed from the hashed lock, not the developer environment.
    lock = Path(__file__).with_name("storage-requirements-lock.txt")
    expected = {line.split("==")[0].lower().replace("_", "-"): line.split("==")[1].split()[0]
                for line in lock.read_text().splitlines() if "==" in line}
    actual = {d.metadata["Name"].lower().replace("_", "-"): d.version for d in metadata.distributions()}
    if actual != expected:
        raise RuntimeError("Build environment must match storage-requirements-lock.txt exactly")
    command = [sys.executable, "-m", "PyInstaller", "--onedir", "--console", "--noupx",
               "--name", "Qiban.Storage", "--distpath", str(output), "--workpath", str(work / "build"),
               "--specpath", str(work), "--paths", str(ROOT / "src"),
               "--collect-data", "tzdata",
               "--collect-data", "bilibili_api", "--hidden-import", "bilibili_api.clients.HTTPXClient",
               "--copy-metadata", "bilibili-api-python",
               "--hidden-import", "psycopg_binary", "--hidden-import", "psycopg_pool",
               str(Path(__file__).with_name("storage_worker.py"))]
    # collect-data runs before --paths is applied; source resources are explicit.
    for resource in sorted((ROOT / "src/companion_core").glob("*.json")):
        command[-1:-1] = ["--add-data", str(resource)+";companion_core"]
    command[-1:-1] = ["--add-data", str(ROOT / "src/bili_assistant/defaults/policy.yaml")+";bili_assistant/defaults"]
    command[-1:-1] = ["--add-data", str(ROOT / "src/companion_core/sql")+";companion_core/sql"]
    command[-1:-1] = ["--add-data", str(ROOT / "src/companion_qq/native")+";companion_qq/native"]
    command[-1:-1] = ["--add-data", str(ROOT / "src/companion_wechat/native")+";companion_wechat/native"]
    with (work / "build.log").open("w", encoding="utf8") as log:
        subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, cwd=work)
    bundle = output / "Qiban.Storage"
    check = subprocess.run([str(bundle / "Qiban.Storage.exe"), "--check-resources"],
                           check=True, capture_output=True, text=True, timeout=30, cwd=work)
    if not json.loads(check.stdout)["frozen"]:
        raise RuntimeError("Expected bundled runtime")
    copy_licenses(bundle, lock)
    files = [{"path": p.relative_to(bundle).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in sorted(bundle.rglob("*")) if p.is_file()]
    manifest = {"format": 1, "entrypoint": "Qiban.Storage.exe", "python": sys.version.split()[0],
                "platform": "win-amd64", "files": files}
    (bundle / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
    print(json.dumps({"bundle": str(bundle), "files": len(files),
                      "bytes": sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file())}))


if __name__ == "__main__":
    main()
