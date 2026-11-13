"""Record administration shared by desktop and legacy management transports."""
import secrets
import time
from dataclasses import asdict

import yaml

from .errors import AssistantError
from .governance import resource_id


class RecordManagement:
    def __init__(self, store, admin):
        self.store, self.admin = store, admin
        self.delete_previews = {}
        from .record_import_advanced import AdvancedImport
        self.advanced = AdvancedImport(self)

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
        value = ({**asdict(policy), "owners": sorted(policy.owners), "sources": list(policy.sources),
                  "source_versions": [list(pin) for pin in policy.source_versions]} if policy else None)
        from .fact_retirement import retired
        return {**item, "policy": value,
                "retirement": retired(self.store, identifier) if kind == 'memories' and item['data'].get('fact') else None}

    def correct(self, request, kind, identifier, value, expected):
        self.require(request)
        if kind in self.store.profile.config:
            return self.admin.patch_config(request, kind, value, expected)
        # The store changes policy revision in the same transaction; every
        # derived source pin then fails closed until explicitly reviewed.
        from companion_core import baseline

        with self.store.lock, self.store.db() as db, baseline.manual_edit(self.store):
            db.execute("BEGIN IMMEDIATE")
            self.require(request)
            if kind == "memories" and (value.get("fact") or self.store.exists(kind, identifier)
                    and self.store.read(kind, identifier)["data"].get("fact")):
                raise AssistantError("fact_append_required", "个人事实不能直接覆盖，请在时间线追加管理员更正，原记录和来源会保留")
            fixed = (kind == "memories" and baseline.enabled(self.store)
                     and identifier in baseline.identifiers(self.store.read("persona")["data"]))
            manual_document = kind in ('knowledge', 'diaries')
            if manual_document and self.store.exists(kind, identifier):
                original = self.store.read(kind, identifier)
                if original['data'].get('locked') and value.get('locked') is not True:
                    raise AssistantError('record_lock_preserved', '管理员更正不解除条目锁定，请保留禁止自动修改的设置。')
            result = self.store.write(kind, value, expected, "admin:" + request.actor, identifier,
                                      allow_locked=fixed or manual_document)
            if fixed:
                baseline.validate_manual_edit(self.store)
            self.admin.repository.audit(request.actor, "correct", resource_id(kind, identifier), expected, result["version"])
        return result

    def export(self, request, kind, identifier=None):
        self.require(request)
        item = self.store.read(kind, identifier)
        self.admin.repository.audit(request.actor, "plaintext_export", resource_id(kind, identifier))
        return yaml.safe_dump(item["data"], allow_unicode=True, sort_keys=False)

    def explain(self, request, kind, identifier=None):
        from .record_inspection import explain

        self.require(request)
        if kind not in self.store.profile.schemas:
            raise AssistantError("invalid_query", "资料查询范围无效")
        return explain(self.store, resource_id(kind, identifier))

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
        from .record_import import parse

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
        return {"actors": [{"key": row["actor_key"], **self.store.ledger_decode("actor_nodes.identity", row["identity_body"])}
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
            if kind == 'memories' and record['data'].get('fact'):
                from .fact_retirement import preview
                plan = preview(self.store, identifier)
                token = secrets.token_urlsafe(24)
                self.delete_previews = {token: {'session': request.session_id, 'expires': time.time() + 120,
                    'kind': kind, 'id': identifier, 'fact_retirement': plan['fingerprint']}}
                return {'token': token, 'affected_count': plan['affected_count'],
                        'effect': '将停用同一账号、会话和属性的全部既有事实（含旧值与尚未生效的值），不恢复旧值。历史保留；只有明确停用、过保留期且不再被引用的正文才会清理。'}
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
            if 'fact_retirement' in preview:
                from .fact_retirement import retire
                return retire(self.store, preview['id'], preview['fact_retirement'], request.actor)
            current = impact(self.store, resource_id(preview["kind"], preview["id"]))
            if current["fingerprint"] != preview["impact"]:
                raise AssistantError("edit_conflict", "资料依赖在预览后变化，请重新查看影响")
            return remove(self.store, preview["kind"], preview["id"], preview["version"], request.actor)
