"""Narrow private store port for trusted read-only platform material collectors."""
import secrets

from .access import AccessService, RecordPolicy
from .errors import AssistantError
from .governance import GovernanceRepository, ReadView, resource_id, verify_record_versions


class TaskWorkspace(ReadView):
    WRITABLE = frozenset({"source_snapshots", "video_progress"})

    def __init__(self, store, authority, request, task_id, guard):
        self.repository = GovernanceRepository(store)
        self.access, self.request = AccessService(self.repository, authority), request
        super().__init__(store, self.repository.readable_resources(self.access, request))
        self.scope_key, self.guard = task_id, guard
        root = "task_requests/" + task_id
        self.pins = {root: self.repository.policy(root).version}
        self.versions = {}

    def check(self):
        self.guard()
        lease = self.access.lease(self.request, self.pins)
        current = dict(lease.versions)
        if any(current.get(key) != version for key, version in self.pins.items()):
            raise AssistantError("context_changed", "任务材料权限已变化")
        verify_record_versions(self.store, self.versions)

    def read(self, kind, item_id=None):
        if kind in self.profile.config and item_id is None:
            # Collector controls only; the handler selects the material sent to Codex.
            return self.store.read(kind)
        resource = resource_id(kind, item_id)
        if not self.access.can_read(self.request, resource):
            raise AssistantError("not_found", "资料不存在或不可在当前任务使用")
        lease = self.access.lease(self.request, [resource])
        item = self.store.read(kind, item_id)
        self.access.revalidate(lease)
        self.pins[resource] = dict(lease.versions)[resource]
        self.versions[kind, item_id] = item["version"]
        return item

    def exists(self, kind, item_id=None):
        return self.access.can_read(self.request, resource_id(kind, item_id)) and self.store.exists(kind, item_id)

    def write(self, kind, data, expected, actor, item_id=None, allow_locked=False):
        if kind not in self.WRITABLE or allow_locked:
            raise AssistantError("mode_denied", "任务资料收集器不能修改管理配置或发布内容")
        resource = resource_id(kind, item_id)
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.check()
            if self.store.exists(kind, item_id) and resource not in self.readable:
                raise AssistantError("access_denied", "不能覆盖其他范围的材料")
            prior = self.repository.policy(resource)
            item = self.store.write(kind, data, expected, actor, item_id)
            previous = self.repository.policy(resource)
            # Raw platform material is a new observation, not a paraphrase of
            # mutable progress. Progress retains its prior underlying sources.
            root = "task_requests/" + self.scope_key
            dependencies = ({root: self.pins[root]} if kind == "source_snapshots" else
                            {**dict(prior.source_versions if prior else ()), **self.pins})
            pins = tuple((key, version) for key, version in dependencies.items() if key != resource)
            policy = RecordPolicy(resource, secrets.token_hex(16), "conversation", frozenset((self.request.actor,)),
                self.request.conversation, "private", tuple(key for key, _ in pins), source_versions=pins)
            self.repository.set_policy(policy, previous.version, "private-task-collector")
            self.pins[resource] = policy.version
            self.versions[kind, item_id] = item["version"]
            self.readable.add(resource)
            self.check()
        return item

    def state(self, key, default=None, db=None):
        if key.startswith("context:"):
            key = "private-task:" + self.scope_key + ":" + key
        return self.store.state(key, default, db)

    def set_state(self, key, value, db=None):
        if not key.startswith("context:"):
            raise AssistantError("mode_denied", "材料收集器只能保存当前任务上下文")
        return self.store.set_state("private-task:" + self.scope_key + ":" + key, value, db)
