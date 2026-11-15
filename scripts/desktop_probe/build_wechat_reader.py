"""Freeze the tested SQLCipher reader and verify the matching native bridge."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from build_storage import ROOT, copy_licenses


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "work", "bridge"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    lock = Path(__file__).with_name("wechat-reader-requirements-lock.txt")
    required = {s.split("==")[0]: s.split("==")[1].split()[0] for s in lock.read_text().splitlines()}
    actual = {d.metadata["Name"].lower().replace("_", "-"): d.version for d in metadata.distributions()}
    if actual != required:
        raise RuntimeError("Reader build environment differs from its lock file")
    sys.path.insert(0,str(ROOT/"src"))
    from companion_wechat.native.bridge import validate_build
    bridge = args.bridge.resolve(strict=True)
    validate_build(bridge)
    sys.path.remove(str(ROOT/"src"))
    output, work = args.output.resolve(), args.work.resolve()
    output.mkdir(parents=True,exist_ok=False)
    work.mkdir(parents=True,exist_ok=False)
    name = "Qiban.WeChatReader"
    command = [sys.executable,"-m","PyInstaller","--onedir","--console","--noupx","--name",name,
               "--distpath",str(output),"--workpath",str(work/"build"),"--specpath",str(work),
               "--paths",str(ROOT/"src"),"--copy-metadata","sqlcipher3","--copy-metadata","zstandard",
               "--add-data",str(ROOT/"src/companion_core/assessment_schemas.json")+";companion_core",
               "--add-data",str(ROOT/"src/companion_wechat/native")+";companion_wechat/native",
               str(Path(__file__).with_name("wechat_reader_worker.py"))]
    with (work/"build.log").open("w",encoding="utf8") as log:
        subprocess.run(command,cwd=work,stdout=log,stderr=log,check=True)
    bundle = output/name
    shutil.copy2(bridge,bundle/"bridge.dll")
    shutil.copy2(bridge.with_suffix(".json"),bundle/"bridge.json")
    copy_licenses(bundle,lock)
    for path in (ROOT/"src/companion_wechat/native").glob("*"):
        if path.is_file() and ("LICENSE" in path.name or "NOTICE" in path.name):
            shutil.copy2(path,bundle/"licenses"/path.name)
    files = [{"path":p.relative_to(bundle).as_posix(),"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in sorted(bundle.rglob("*")) if p.is_file()]
    (bundle/"runtime-manifest.json").write_text(json.dumps({"format":1,"entrypoint":name+".exe","files":files},indent=2))
    print(json.dumps({"files":len(files),"bytes":sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file())}))


if __name__=="__main__":
    main()
