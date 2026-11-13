"""Immutable shared provenance branches; never evicts facts or past views."""
from hashlib import sha256

from .access import RecordPolicy
from .contracts import stable_key
from .errors import AssistantError

FANOUT = 16


def persist(repository, db, event, signature, pins):
    scope = (event.actor.key, event.account, event.thread)
    identifier = stable_key('fact-view', *scope, signature, pins)

    def policy(key, sources):
        return RecordPolicy('personal-fact-view/' + key, key, 'personal', frozenset((event.actor.key,)),
                            event.thread, 'private', tuple(source for source, _ in sources),
                            source_versions=tuple(sources))

    flat = policy(identifier, pins)
    # Existing flat snapshots remain immutable and readable across an upgrade.
    existing = repository.policy(flat.resource)
    if existing == flat:
        return flat.resource

    def put(value):
        current = repository.policy(value.resource)
        if current is None:
            repository.set_policy(value, None, 'personal-fact-projection', db=db)
        elif current != value:
            raise AssistantError('context_changed', '事实视图的来源节点已经变化')
        return value.resource, value.version

    def branches(items, depth):
        if len(items) <= FANOUT:
            return [(resource, version) for _, resource, version in items]
        groups = {}
        for digest, resource, version in items:
            # Hash partitioning keeps unrelated branches stable when an ID is inserted.
            if depth == len(digest):
                raise AssistantError('context_changed', '事实视图无法按来源分组')
            groups.setdefault(digest[depth], []).append((digest, resource, version))
        result = []
        for key in sorted(groups):
            group = groups[key]
            if len(group) == 1:
                result.append((group[0][1], group[0][2]))
                continue
            sources = branches(group, depth + 1)
            part = stable_key('fact-view-part', *scope, sources)
            result.append(put(policy(part, sources)))
        return result

    items = [(sha256(resource.encode('utf8')).hexdigest(), resource, version) for resource, version in pins]
    root = policy(identifier, branches(items, 0))
    put(root)
    return root.resource
