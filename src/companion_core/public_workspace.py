"""A public-material workspace for autonomous exploration and maintenance.

Private replies use GovernedContext separately. This facade reuses existing
learning/retrieval algorithms while excluding private records before they scan
or rank. Operational settings stay internal; callers use model_config for output.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import nullcontext
from copy import deepcopy

from .access import AccessService, RecordPolicy, RequestAuthority
from .errors import AssistantError
from .governance import GovernanceRepository, ReadView, resource_id
from .provenance import freeze, rebase, routine_update


class PublicWorkspace(ReadView):
    is_public_workspace = True
    INTERNAL = {"manifest", "policy", "preferences", "companion"}
    SCOPED_STATE = ("reflection-", "emotion-pending:", "search-expansion:", "search-queries:",
                    "semantic-", "recall-")

    def __init__(self, store):
        self.repository = GovernanceRepository(store)
        self.authority = RequestAuthority(epoch=self.repository.epoch)
        self.request = self.authority.issue(actor=None, conversation="public-autonomy", audience="public",
                                            purpose="exploration", lifetime=3600)
        self.access = AccessService(self.repository, self.authority)
        super().__init__(store, self.repository.readable_resources(self.access, self.request))
        self.known, self.direct, self.leases = {}, set(), []
        self.sources = {}
        self.baseline_guards = []

    def _remember(self, kind, item):
        resource = resource_id(kind, item["data"].get("id") if kind in self.profile.collections else None)
        self.known.setdefault(item["data"].get("id", "@" + kind), set()).add(resource)

    def records(self, kind):
        result = super().records(kind)
        for item in result[0]:
            self._remember(kind, item)
        return result

    def read(self, kind, item_id=None):
        if kind in self.INTERNAL and item_id is None:
            return self.store.read(kind)
        if kind == "growth" and item_id is None:
            result = self.store.state("public-growth")
            if result is None:
                data = deepcopy(self.profile.defaults["growth"])
                return {"data": data, "version": self._version(data)}
            policy = self.repository.policy("partition/public-growth")
            if policy is None or not self.access.can_read(self.request, policy.resource):
                raise AssistantError("access_denied", "公开成长状态的来源已失效")
            self.direct.add(policy.resource)
            return result
        if kind == "persona" and resource_id(kind) not in self.readable:
            from .baseline import enabled

            if enabled(self):
                raise AssistantError("baseline_unavailable", "共用人物卡不可用，未替换为默认人设")
            return {"data": deepcopy(self.profile.defaults["persona"]), "version": self.store.version(kind)}
        if resource_id(kind, item_id) not in self.readable:
            raise AssistantError("not_found", "资料不存在或不属于公开工作区")
        item = self.store.read(kind, item_id)
        self._remember(kind, item)
        self.direct.add(resource_id(kind, item_id))
        return item

    def exists(self, kind, item_id=None):
        if kind == "growth" and item_id is None:
            return True
        return resource_id(kind, item_id) in self.readable and self.store.exists(kind, item_id)

    def version(self, kind, item_id=None):
        try:
            return self.read(kind, item_id)["version"]
        except AssistantError:
            return None

    def state(self, key, default=None, db=None):
        if key.startswith(self.SCOPED_STATE):
            key = "public-state:" + key
        return self.store.state(key, default, db)

    def set_state(self, key, value, db=None):
        if key.startswith(self.SCOPED_STATE):
            key = "public-state:" + key
        return self.store.set_state(key, value, db)

    @staticmethod
    def _version(data):
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def model_config(self, kind):
        data = self.read(kind)["data"]
        if kind == "preferences":
            return {"blocked_targets_applied_by_service": True}
        if kind == "companion":
            return {key: value for key, value in data.items() if isinstance(value, (bool, int, float))}
        if kind == "policy":
            return {key: value for key, value in data.items() if key in ("features", "daily_model_cycles", "items_per_cycle")}
        if kind == "manifest":
            return {}
        return data

    def before_model(self, context):
        resources = set(self.direct)

        def visit(value):
            if isinstance(value, dict):
                identifier = value.get("id")
                if isinstance(identifier, str):
                    resources.update(self.known.get(identifier, ()))
                for child in value.values():
                    visit(child)
            elif isinstance(value, (tuple, list)):
                for child in value:
                    visit(child)

        visit(context)
        lease, sources = freeze(self.access, self.access.lease(self.request, resources))
        self.leases.append(lease)
        self.sources = dict(sources)

        def guard():
            self.check()

        return guard

    def check(self):
        for guard in self.baseline_guards:
            guard()
        for lease in self.leases:
            self.access.revalidate(lease)

    def _classify(self, resource):
        previous = self.repository.policy(resource)
        pins = tuple((key, value) for key, value in self.sources.items() if key != resource)
        self.repository.set_policy(RecordPolicy(resource, secrets.token_hex(16), "public",
                                   sources=tuple(key for key, _ in pins), source_versions=pins),
                                   previous.version if previous else None, "public-workspace")
        policy = self.repository.policy(resource)
        self.leases = [rebase(self.access, lease, resource, policy.version, self.sources.items())
                       for lease in self.leases]
        if resource in self.sources:
            self.sources[resource] = policy.version
        self.readable.add(resource)

    def write(self, kind, data, expected, actor, item_id=None, allow_locked=False):
        self.check()
        if kind in self.profile.config:
            raise AssistantError("mode_denied", "公开工作区不能修改管理配置")
        with self.store.lock, self.store.db() as db, (
                routine_update(self.store, "partition/public-growth") if kind == "growth" else nullcontext()):
            if getattr(db, "dialect", "sqlite") == "postgres":
                db.execute("BEGIN IMMEDIATE")
            self.check()
            if kind == "growth":
                if self.read(kind)["version"] != expected:
                    raise AssistantError("edit_conflict", "公开成长状态已经变化")
                self.profile.validate(kind, data)
                result = {"data": deepcopy(data), "version": self._version(data)}
                self.store.set_state("public-growth", result)
                self._classify("partition/public-growth")
                return result
            resource = resource_id(kind, item_id)
            if self.store.exists(kind, item_id) and resource not in self.readable:
                raise AssistantError("access_denied", "不能覆盖尚未确认可见范围的旧资料")
            result = self.store.write(kind, data, expected, actor, item_id, allow_locked=allow_locked)
            self._classify(resource)
            self._remember(kind, result)
            return result

    def validate_critical(self):
        # This checks operational validity without disclosing configs to a model.
        return self.store.validate_critical()
