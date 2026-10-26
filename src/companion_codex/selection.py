"""Durable Qiban model choices, independent of the user's Codex configuration."""
import re
import uuid

from companion_core.errors import AssistantError

from .isolation import model_settings

KEY = "qiban-model-selection-v1"
SCOPES = ("global", "local", "bilibili", "qq", "wechat")


def require(ok, code="model_selection_invalid", message="模型设置无效，请重新读取设置"):
    if not ok:
        raise AssistantError(code, message)


class ModelSelection:
    def __init__(self, store, host):
        self.store, self.host = store, host

    def read(self):
        return self.store.state(KEY, {"version": None, "global": None, "overrides": {}})

    def resolve(self, scope, *, saved=None):
        require(scope in SCOPES)
        saved = self.read() if saved is None else saved
        choice = saved["overrides"].get(scope) if scope != "global" else None
        source = scope if choice else "global"
        choice = choice or saved["global"]
        if choice is None:
            inherited = model_settings()
            choice = {"model": inherited.get("model"), "effort": inherited.get("model_reasoning_effort")}
            source = "codex_config"
        return {**choice, "source": source, "version": saved["version"]}

    def snapshot(self):
        saved = self.read()
        return {**saved, "effective": {scope: self.resolve(scope, saved=saved) for scope in SCOPES},
                "applies": "next_model_request", "changes_codex_config": False}

    def catalog(self):
        models, seen, cursor = {}, set(), None
        for _ in range(20):
            page = self.host.request("model/list", {"limit": 100, "includeHidden": False,
                                                   **({"cursor": cursor} if cursor else {})})
            require(isinstance(page, dict) and isinstance(page.get("data"), list), "model_catalog_invalid")
            for item in page["data"]:
                require(isinstance(item, dict), "model_catalog_invalid")
                model = item.get("model")
                require(isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", model),
                        "model_catalog_invalid")
                if item.get("hidden"):
                    continue
                efforts = [v.get("reasoningEffort") for v in item.get("supportedReasoningEfforts", [])
                           if isinstance(v, dict)]
                require(all(isinstance(v, str) and re.fullmatch(r"[a-z_]{1,30}", v) for v in efforts),
                        "model_catalog_invalid")
                default = item.get("defaultReasoningEffort")
                require(default is None or default in efforts, "model_catalog_invalid")
                models[model] = {"model": model, "label": str(item.get("displayName") or model)[:150],
                                 "efforts": efforts, "default_effort": default,
                                 "is_default": bool(item.get("isDefault"))}
            cursor = page.get("nextCursor")
            if cursor is None:
                return {"items": list(models.values())}
            require(isinstance(cursor, str) and cursor and cursor not in seen, "model_catalog_invalid")
            seen.add(cursor)
        raise AssistantError("model_catalog_invalid", "模型目录分页异常，请稍后重试")

    def update(self, *, scope, model, effort, expected_version, actor, audit):
        require(isinstance(scope, str) and scope in SCOPES)
        require(model is None or isinstance(model, str))
        require(effort is None or isinstance(effort, str))
        require(expected_version is None or isinstance(expected_version, str))
        choice = None
        if model is not None:
            available = {item["model"]: item for item in self.catalog()["items"]}
            require(model in available, "model_unavailable", "该模型未在当前宿主目录中，请刷新可选模型")
            item = available[model]
            effort = item["default_effort"] if effort is None else effort
            require(effort is None or effort in item["efforts"], "model_effort_invalid", "该模型不支持所选推理档位")
            choice = {"model": model, "effort": effort}
        else:
            require(effort is None)
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = self.store.state(KEY, {"version": None, "global": None, "overrides": {}}, db)
            require(saved["version"] == expected_version, "edit_conflict", "模型设置已改变，请重新读取后保存")
            previous = saved["version"]
            if scope == "global":
                saved["global"] = choice
            elif choice is None:
                saved["overrides"].pop(scope, None)
            else:
                saved["overrides"][scope] = choice
            saved["version"] = uuid.uuid4().hex
            self.store.set_state(KEY, saved, db)
            audit(actor, "model_settings", scope, previous, saved["version"], db=db)
        return self.snapshot()
