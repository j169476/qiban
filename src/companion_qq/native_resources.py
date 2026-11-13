"""Pinned local native resources; account files never belong in this directory."""
import hashlib
import json
import os
import shutil
from pathlib import Path

from companion_core.errors import AssistantError


def runtime_lock():
    return json.loads(Path(__file__).with_name("native").joinpath("runtime-lock.json").read_text(encoding="utf8"))


def matches(path, entry):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != entry["size"]:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == entry["sha256"]


def verify_runtime(directory, *, execution=False):
    root = Path(directory)
    lock = runtime_lock()
    expected = {entry["path"] for entry in lock["files"]}
    present = {p.name for p in root.iterdir()} if root.is_dir() else set()
    allowed = expected | ({"beacon_report.log"} if execution else set())
    if (root.is_symlink() or not root.is_dir() or not expected <= present or not present <= allowed
            or not all(matches(root / entry["path"], entry) for entry in lock["files"])):
        raise AssistantError("qq_native_resources_invalid", "QQ 运行资源缺失或与固定版本不一致，请重新准备组件。")
    return {"node": lock["node"], "qq": lock["qq"], "files": len(lock["files"])}


def prepare_local_runtime(source, target, *, execution=False):
    """Explicit local assembly. Copy only locked files; resume interrupted copies.

    This does not grant redistribution rights or copy an installation's account data.
    A failed staging folder is retained for an explicit retry, never launched.
    """
    source, target = Path(source), Path(target)
    lock = runtime_lock()
    if source.resolve() == target.resolve() or target.is_symlink():
        raise AssistantError("qq_native_resources_path", "请选择独立的 QQ 组件目录。")
    target.mkdir(parents=True, exist_ok=True)
    allowed = {entry["path"] for entry in lock["files"]}
    if execution:
        allowed.add("beacon_report.log")
    if any(p.name not in allowed | {name + ".part" for name in allowed} for p in target.iterdir()):
        raise AssistantError("qq_native_resources_invalid", "组件目录中存在其他文件，请使用独立目录。")
    for entry in lock["files"]:
        original, destination = source / entry["path"], target / entry["path"]
        partial = destination.with_name(destination.name + ".part")
        if destination.is_symlink() or partial.is_symlink():
            raise AssistantError("qq_native_resources_path", "组件文件不能使用链接。")
        if matches(destination, entry) and not partial.exists():
            continue
        if not matches(original, entry):
            raise AssistantError("qq_native_resources_invalid", "来源文件与固定版本不一致。")
        with original.open("rb") as incoming, partial.open("wb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if not matches(partial, entry):
            raise AssistantError("qq_native_resources_invalid", "组件复制校验失败。")
        partial.replace(destination)
    return verify_runtime(target, execution=execution)


def working_directory(runtime, data):
    runtime, data = Path(runtime).resolve(strict=True), Path(data).resolve()
    if runtime == data or runtime in data.parents or data in runtime.parents:
        raise AssistantError("qq_native_resources_path", "QQ 运行资源与账号数据必须放在独立目录。")
    working = data / "work"
    working.mkdir(parents=True, exist_ok=True)
    if working.is_symlink() or working.resolve().parent != data:
        raise AssistantError("qq_native_resources_path", "QQ 工作目录必须位于账号数据目录中。")
    return working


def execution_runtime(runtime, data):
    """The pinned QQ library writes beacon_report.log beside itself even with a separate cwd.

    Keep the distributable pack immutable; load verified copies under account data.
    Only that observed log is allowed beside the execution copies, never in the pack.
    """
    verify_runtime(runtime)
    working = working_directory(runtime, data)
    execution = working.parent / "native-runtime"
    prepare_local_runtime(runtime, execution, execution=True)
    return execution, working
