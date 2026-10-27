"""Management operations shared by the authenticated UI and trusted bridge."""
import hashlib
import hmac
import secrets
import time
from dataclasses import asdict

import yaml

from companion_core.errors import AssistantError
from companion_core.governance import resource_id


class Management:
    def __init__(self, service):
        self.service, self.admin, self.store = service, service.admin, service.store
        self.restore_previews = {}
        self.delete_previews = {}
        self.growth_previews = {}

    def backups(self, request):
        self.require(request)
        folder = self.store.root / "history" / "backups"
        return [{"name": path.name, "bytes": path.stat().st_size} for path in sorted(folder.glob("*.ccbackup"))
                if path.is_file() and not path.is_symlink() and path.resolve().parent == folder.resolve()]

    def backup_path(self, name):
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or not name.endswith(".ccbackup"):
            raise AssistantError("backup_path", "备份名称无效")
        folder = self.store.root / "history" / "backups"
        path = folder / name
        if not path.is_file() or path.is_symlink() or path.resolve().parent != folder.resolve():
            raise AssistantError("backup_path", "备份不存在")
        return path

    def restore_preview(self, request, name):
        with self.service.state_lock:
            return self._restore_preview(request, name)

    def _restore_preview(self, request, name):
        from companion_core.postgres_transfer import fingerprint, unpack

        self.require(request)
        if (self.service.busy or self.service.platform_runs.busy or self.service.backups.busy or self.service.backup_active
                or self.service.notice_task is not None and not self.service.notice_task.done()):
            raise AssistantError("worker_busy", "先暂停角色并等待当前任务退出")
        self.service._pause("restore_preview")
        self.service.maintenance_until = time.time() + 120
        if self.service.supervisor:
            self.service.supervisor.pulse()
        try:
            raw = self.backup_path(name).read_bytes()
            snapshot = unpack(raw, self.store._backup_key)
        except Exception:
            self.service.maintenance_until = 0
            raise
        token = secrets.token_urlsafe(24)
        self.restore_previews = {token: {"actor": request.actor, "session": request.session_id,
            "name": name, "hash": hashlib.sha256(raw).hexdigest(), "expected": fingerprint(self.store),
            "expires": time.time() + 120}}
        return {"token": token, "expires_in_seconds": 120, "name": name,
                "counts": {table: len(block["rows"]) for table, block in snapshot["tables"].items()},
                "effect": "恢复覆盖当前数据；全部网页会话和跨账号授权失效，角色保持暂停。"}

    def restore(self, request, token, password):
        from companion_core.postgres_transfer import restore
        from companion_core.sessions import password_hash

        self.require(request)
        preview = self.restore_previews.pop(token, None)
        owner = self.store.state("web-owner", {})
        if (not preview or preview["actor"] != request.actor or preview["session"] != request.session_id
                or preview["expires"] <= time.time() or not owner
                or not hmac.compare_digest(owner["hash"], password_hash(password, owner["salt"]))):
            raise AssistantError("restore_denied", "恢复预览或密码无效，请重新检查")
        raw = self.backup_path(preview["name"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != preview["hash"]:
            raise AssistantError("edit_conflict", "备份在预览后发生变化")
        try:
            return restore(self.store, raw, self.store._backup_key, expected=preview["expected"])
        finally:
            self.service.maintenance_until = 0

    def require(self, request):
        self.admin.authority.require(request, administrator=True)

    def listing(self, request, kind, *, after="", limit=50):
        self.require(request)
        if kind not in self.store.profile.schemas or not isinstance(after, str) or not 1 <= limit <= 100:
            raise AssistantError("invalid_query", "资料查询范围无效")
        with self.store.db() as db:
            rows = db.execute("SELECT r.id,r.version,p.classification FROM content_records r "
                              "LEFT JOIN governance_policies p ON p.resource=? || r.id "
                              "WHERE r.kind=? AND r.id>? ORDER BY r.id LIMIT ?",
                              (kind + "/", kind, after, limit + 1)).fetchall()
        return {"records": [dict(row) for row in rows[:limit]],
                "next": rows[limit - 1]["id"] if len(rows) > limit else None}

    def detail(self, request, kind, identifier=None):
        self.require(request)
        item = self.store.read(kind, identifier)
        policy = self.admin.repository.policy(resource_id(kind, identifier))
        return {**item, "policy": asdict(policy) if policy else None}

    def correct(self, request, kind, identifier, value, expected):
        self.require(request)
        if kind in self.store.profile.config:
            return self.admin.patch_config(request, kind, value, expected)
        # The store changes policy revision in the same transaction; every
        # derived source pin then fails closed until explicitly reviewed.
        from companion_core import baseline

        with self.store.lock, self.store.db() as db, baseline.manual_edit(self.store):
            db.execute("BEGIN IMMEDIATE")
            fixed = (kind == "memories" and baseline.enabled(self.store)
                     and identifier in baseline.identifiers(self.store.read("persona")["data"]))
            result = self.store.write(kind, value, expected, "admin:" + request.actor, identifier, allow_locked=fixed)
            if fixed:
                baseline.validate_manual_edit(self.store)
            self.admin.repository.audit(request.actor, "correct", resource_id(kind, identifier), expected, result["version"])
        return result

    def export(self, request, kind, identifier=None):
        self.require(request)
        item = self.store.read(kind, identifier)
        self.admin.repository.audit(request.actor, "plaintext_export", resource_id(kind, identifier))
        return yaml.safe_dump(item["data"], allow_unicode=True, sort_keys=False)

    def add_foundation(self, request, identifier, content, expected):
        from companion_core import baseline
        from companion_core.storage import utcnow

        self.require(request)
        if not baseline.enabled(self.store):
            raise AssistantError("baseline_unavailable", "请先确认并登记共用设定")
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            card = self.store.read("persona")
            if card["version"] != expected:
                raise AssistantError("edit_conflict", "人物卡已修改，请重新读取")
            if self.store.exists("memories", identifier):
                raise AssistantError("baseline_exists", "该 ID 已存在，请编辑已有基础记忆或使用新 ID")
            self.correct(request, "memories", identifier, {"schema_version": 1, "id": identifier,
                "kind": "user_preference", "content": content, "confidence": "observed", "tags": [], "locked": True,
                "source": {"type": "user_instruction", "recorded_at": utcnow(), "reference": "manual-shared-baseline"}}, None)
            self.admin.patch_config(request, "persona", {
                "foundation_memories": [*baseline.identifiers(card["data"]), identifier]}, expected)
        return {"added": identifier, "shared": True}

    def import_preview(self, request, kind, identifier, text, expected_version):
        from .yaml_import import parse

        self.require(request)
        current = self.store.read(kind, identifier)
        if current["version"] != expected_version:
            raise AssistantError("edit_conflict", "资料已经变化，请重新读取再导入")
        value = parse(text)
        self.store.profile.validate(kind, value)
        if value.get("id") != current["data"].get("id"):
            raise AssistantError("yaml_import", "只能替换当前选择的资料，不能更换 ID")
        return {"data": value, "expected_version": current["version"], "written": False}

    def actors(self, request):
        self.require(request)
        with self.store.db() as db:
            nodes = db.execute("SELECT * FROM actor_nodes ORDER BY actor_key LIMIT 200").fetchall()
            claims = db.execute("SELECT id,subject,object,declarer,status,version FROM relationship_claims ORDER BY id LIMIT 200").fetchall()
        return {"actors": [{"key": row["actor_key"], **self.store.ledger_decode("actor_nodes.identity", row["identity"])}
                           for row in nodes], "relations": [dict(row) for row in claims], "limit": 200}

    def delete_preview(self, request, kind, identifier):
        from companion_core import baseline
        from companion_core.record_inspection import impact

        self.require(request)
        if kind not in self.store.profile.collections:
            raise AssistantError("invalid_delete", "配置不能从此处删除")
        if kind == "memories" and identifier in baseline.identifiers(self.store.read("persona")["data"]):
            raise AssistantError("baseline_locked", "请先从共用设定移除这条基础记忆")
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.store.read(kind, identifier)
            affected = impact(self.store, resource_id(kind, identifier))
            token = secrets.token_urlsafe(24)
            self.delete_previews = {token: {"session": request.session_id, "expires": time.time() + 120,
                "kind": kind, "id": identifier, "version": record["version"], "impact": affected["fingerprint"]}}
        return {"token": token, **affected,
                "effect": "删除当前条目并停止其派生资料的读取；加密历史和既有备份保留，未承诺物理抹除。"}

    def delete(self, request, token):
        from companion_core.record_inspection import impact
        from companion_core.record_maintenance import remove

        self.require(request)
        preview = self.delete_previews.pop(token, None)
        if not preview or preview["session"] != request.session_id or preview["expires"] <= time.time():
            raise AssistantError("delete_denied", "删除预览失效，请重新查看")
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require(request)
            current = impact(self.store, resource_id(preview["kind"], preview["id"]))
            if current["fingerprint"] != preview["impact"]:
                raise AssistantError("edit_conflict", "资料依赖在预览后变化，请重新查看影响")
            return remove(self.store, preview["kind"], preview["id"], preview["version"], request.actor)

    def operation(self, request, operation, args):
        self.require(request)
        if operation in {"public_growth_preview", "public_growth_rebuild"}:
            from companion_core import public_growth

            with self.service.state_lock, self.store.lock:
                run = self.store.state("core-run:bilibili", {})
                if run.get("status") == "active" or "bilibili" in self.service.platform_runs.busy:
                    raise AssistantError("worker_busy", "先暂停B站并等待当前任务退出")
                if operation == "public_growth_preview":
                    if args:
                        raise AssistantError("invalid_operation", "公开成长预览不接受参数")
                    preview = public_growth.preview(self.store)
                    token = secrets.token_urlsafe(24)
                    self.growth_previews = {token: {"actor": request.actor, "session": request.session_id,
                        "expected": preview["expected"], "expires": time.time() + 120}}
                    return {**preview, "token": token, "expires_in_seconds": 120}
                if set(args) != {"token"}:
                    raise AssistantError("invalid_operation", "请使用公开成长预览令牌")
                preview = self.growth_previews.pop(args["token"], None)
                if (not preview or preview["actor"] != request.actor or preview["session"] != request.session_id
                        or preview["expires"] <= time.time()):
                    raise AssistantError("edit_conflict", "公开成长预览已失效，请重新预览")
                return public_growth.rebuild(self.store, preview["expected"], request.actor)
        if operation in {"platform_setup_status", "platform_setup_run"}:
            return self.service.setup.operation(request, operation, args)
        if operation in {"model_catalog", "model_settings_get", "model_settings_set"}:
            return self.service.model_operation(request, operation, args)
        if operation == "open_codex":
            if args:
                raise AssistantError("invalid_operation", "打开 Codex 不接受额外参数")
            return self.service.host.open_desktop()
        if operation == "runtime_monitor":
            from .monitoring import snapshot

            if set(args) - {"days"}:
                raise AssistantError("invalid_control", "运行监控只接受统计天数")
            return snapshot(self.service, days=args.get("days", 1))
        if operation == "platform_control":
            if set(args) != {"platform", "action", "expected_generation", "options"} or not isinstance(args["options"], dict):
                raise AssistantError("invalid_control", "平台控制参数无效")
            result = self.service.manage_platform(args["platform"], args["action"], args["expected_generation"], args["options"])
            self.admin.repository.audit(request.actor, "platform_" + args["action"], args["platform"])
            return result
        if operation == "task_cancel":
            if set(args) != {"id", "expected_revision"} or not isinstance(args["id"], str):
                raise AssistantError("invalid_control", "任务取消参数无效")
            with self.store.lock, self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                state = self.service.tasks._state(args["id"])
                if state["revision"] != args["expected_revision"]:
                    raise AssistantError("edit_conflict", "任务状态已变化，请刷新")
                if state["status"] not in {"queued", "running"}:
                    raise AssistantError("task_control", "该任务已经结束")
                result = self.service.tasks._save(args["id"], {**state, "status": "cancelled", "error": "admin_cancelled"})
                self.admin.repository.audit(request.actor, "task_cancel", args["id"])
                return result
        if operation in {"reply_authorization_list", "reply_authorization_set", "reply_policy_list", "reply_policy_set",
                         "reply_policy_object_set", "reply_policy_object_reset",
                         "reply_import_preview", "reply_import_confirm"}:
            from .reply_management import ReplyManagement

            return ReplyManagement(self.service).operation(request, operation, args)
        if operation in {"personal_fact_history", "personal_fact_confirm"}:
            from companion_core.fact_management import FactManagement

            facts = FactManagement(self.admin)
            if operation == "personal_fact_history":
                return facts.history(request, args["id"], after=args.get("after", ""),
                                     limit=args.get("limit", 20), at=args.get("at"))
            return facts.confirm(request, args["id"], args["revision"], args["reason"], args["supersedes"])
        if operation in {"reply_traces", "reply_trace"}:
            from companion_core.reply_trace import detail, listing

            return (listing(self.store, args.get("limit", 30)) if operation == "reply_traces" else
                    detail(self.store, args.get("id"), self.admin.access, request))
        if operation == "record_explain":
            from companion_core.record_inspection import explain

            return explain(self.store, resource_id(args["kind"], args.get("id")))
        if operation == "delete_preview":
            return self.delete_preview(request, args["kind"], args["id"])
        if operation == "record_delete":
            return self.delete(request, args["token"])
        if operation == "grant_list":
            with self.store.db() as db:
                rows = db.execute("SELECT id,grantee,owner,resources,purposes,expires_at,revoked FROM governance_grants "
                                  "ORDER BY created_at DESC LIMIT 200").fetchall()
            return [dict(row) for row in rows]
        if operation in {"baseline_preview", "baseline_publish"}:
            from companion_core import baseline

            if operation == "baseline_preview":
                return {**baseline.preview(self.store), "shared": baseline.enabled(self.store)}
            return baseline.publish(self.store, args["expected"], request.actor)
        if operation == "foundation_add":
            return self.add_foundation(request, args["id"], args["content"], args["expected_version"])
        if operation == "config_patch":
            return self.admin.patch_config(request, args["kind"], args["changes"], args["expected_version"])
        if operation == "record_correct":
            return self.correct(request, args["kind"], args.get("id"), args["data"], args["expected_version"])
        if operation == "yaml_preview":
            return self.import_preview(request, args["kind"], args.get("id"), args["text"], args["expected_version"])
        if operation == "classify":
            return self.admin.classify(request, **args)
        if operation == "authorize":
            return self.admin.authorize(request, **args)
        if operation == "revoke":
            return self.admin.revoke(request, args["grant_id"])
        if operation == "relation_adjudicate":
            from companion_core.relations import RelationService

            return RelationService(self.admin.repository, self.admin.authority).adjudicate(
                request, args["id"], args["status"], args["expected_version"])
        if operation == "pause":
            self.service._pause("admin_web_stop")
            return self.service.status()
        if operation == "backup":
            return self.store.backup(force=True)
        if operation == "backup_list":
            return self.backups(request)
        if operation == "restore_preview":
            return self.restore_preview(request, args["name"])
        if operation == "restore":
            return self.restore(request, args["token"], args["password"])
        raise AssistantError("unknown_management_operation", "未知管理操作")
