"""Authenticated desktop operations for library version history."""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from companion_core.errors import AssistantError
from companion_core.file_library import MAX_BYTES
from companion_core.library_versions import LibraryVersions

FIELDS = {
    "library.versions": {"layer", "identifier", "after"},
    "library.version": {"layer", "identifier", "revision"},
    "library.restore": {"layer", "identifier", "revision", "expected"},
    "library.replace": {"layer", "identifier", "expected", "path"},
    "library.export": {"layer", "identifier", "revision", "path"},
}


def operate(manager, request, op, args):
    manager.authority.require(request, administrator=True)
    versions = LibraryVersions(manager.store, args["layer"])
    for key in FIELDS[op] & {"revision", "identifier", "expected"}:
        text = args[key]
        if not isinstance(text, str) or len(text) != 32 or any(c not in "0123456789abcdef" for c in text):
            raise AssistantError("management_request", "资料或版本标识无效。")
    identifier = args["identifier"]
    if op == "library.versions":
        return versions.listing(identifier, args["after"])
    if op == "library.version":
        return versions.info(identifier, args["revision"])
    if op == "library.restore":
        return versions.save(identifier, args["expected"], actor=request.actor, revision=args["revision"])
    if not isinstance(args["path"], str) or not Path(args["path"]).is_absolute():
        raise AssistantError("management_request", "请在文件选择器中选择位置。")
    path = Path(args["path"])
    if op == "library.replace":
        try:
            with path.open("rb") as reader:
                content = reader.read(MAX_BYTES + 1)
        except (ValueError, OSError):
            raise AssistantError("file_library_read", "无法读取所选文件。") from None
        manager.authority.require(request, administrator=True)
        return versions.save(identifier, args["expected"], actor=request.actor, content=content)
    content = versions.read_version(identifier, args["revision"])
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as writer:
            temporary = Path(writer.name)
            writer.write(content)
        manager.authority.require(request, administrator=True)
        os.replace(temporary, path)
    except (ValueError, OSError):
        raise AssistantError("export_failed", "无法保存所选版本。") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    manager.repository.audit(request.actor, "library_version_exported", versions.prefix + identifier)
    return {"saved": True}
