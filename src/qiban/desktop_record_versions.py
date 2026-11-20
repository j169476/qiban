"""Desktop editing of the text of a record, retaining its current identity and sources."""
from companion_core import record_versions
from companion_core.errors import AssistantError
from companion_core.fact_management import FactManagement

FIELDS = {
    "records.versions": {"kind", "identifier", "after", "limit"},
    "records.version": {"kind", "identifier", "version"},
    "records.restore_version": {"kind", "identifier", "version", "expected"},
    "facts.restore_version": {"identifier", "version", "expected", "reason", "supersedes"},
    "relations.text_preview": {"identifier", "expected", "relation", "declared_reason", "reason"},
    "relations.restore_preview": {"identifier", "sequence", "side", "expected", "reason"},
}


def operate(manager, request, operation, args):
    manager.authority.require(request, administrator=True)
    if operation == "facts.restore_version":
        return FactManagement(manager.admin).restore(request, **args)
    if operation == "relations.text_preview":
        return manager.relations.text_preview(request, **args)
    if operation == "relations.restore_preview":
        return manager.relations.restore_preview(request, **args)
    kind, identifier = args["kind"], args["identifier"]
    if kind not in ("memories", "knowledge", "diaries") or not isinstance(identifier, str) or not 1 <= len(identifier) <= 200:
        raise AssistantError("invalid_query", "请选择一份记忆、知识或日记")
    store = manager.store
    with store.lock, store.db() as db:
        if operation == "records.restore_version":
            db.execute("BEGIN IMMEDIATE")
        now = store.read(kind, identifier)
        if now["data"].get("fact"):
            raise AssistantError("fact_append_required", "请在个人事实时间线中操作")
        if operation == "records.versions":
            result = record_versions.page(store, **args)
        else:
            old = record_versions.read(store, kind, identifier, args["version"])
            result = old
            if operation == "records.restore_version":
                if args["expected"] != now["version"]:
                    raise AssistantError("edit_conflict", "资料已修改，请重新读取")
                value = dict(now["data"])
                for key in ("content", "tags", "title"):
                    if key in old["data"]:
                        value[key] = old["data"][key]
                    else:
                        value.pop(key, None)
                if value != now["data"]:
                    manager.records.correct(request, kind, identifier, value, now["version"])
                result = manager.records.detail(request, kind, identifier)
        manager.authority.require(request, administrator=True)
        return result
