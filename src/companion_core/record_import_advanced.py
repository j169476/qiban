"""Explicit, versioned record import with a single atomic provenance update."""
import hashlib
import json
import secrets
import time
from copy import deepcopy
from dataclasses import asdict, replace

import yaml

from .errors import AssistantError
from .provenance import reserved
from .record_import import MAX_BYTES, parse
from .record_inspection import impact

FORMAT = 'qiban-record-1'
KINDS = {'memories', 'knowledge', 'diaries'}
MAX_REFS = 60


def fail(message):
    raise AssistantError('record_import', message)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AdvancedImport:
    def __init__(self, manager):
        self.manager, self.store = manager, manager.store
        self.repository = manager.admin.repository
        self.previews = {}

    def target(self, request, kind, identifier):
        self.manager.require(request)
        if kind not in KINDS:
            fail('高级导入仅支持普通记忆、知识和日记。')
        item = self.store.read(kind, identifier)
        if item['data'].get('fact'):
            fail('个人事实请在时间线追加更正，不能通过文件覆盖。')
        policy = self.repository.policy(kind + '/' + identifier)
        if policy is None:
            fail('当前资料的来源与权限记录缺失，请先检查恢复。')
        return item, policy

    def content_version(self, resource):
        kind, _, identifier = resource.partition('/')
        if kind in self.store.profile.collections and identifier and self.store.exists(kind, identifier):
            return self.store.read(kind, identifier)['version']
        if kind == 'event' and identifier:
            with self.store.db() as db:
                row = db.execute('SELECT version,deleted FROM core_events WHERE id=?', (identifier,)).fetchone()
            if row and not row['deleted']:
                return row['version']
        return None

    def export(self, request, kind, identifier):
        with self.store.lock, self.store.db() as db:
            item, policy = self.target(request, kind, identifier)
            if len(policy.sources) > MAX_REFS:
                fail('当前资料的直接引用超过60项，请先整理后再导出。')
            pins = dict(policy.source_versions)
            if set(pins) != set(policy.sources):
                fail('来源版本记录不完整，请先检查恢复。')
            envelope = {'format': FORMAT, 'kind': kind, 'record_version': item['version'], 'policy_version': policy.version,
                        'data': item['data'],
                        'references': [{'resource': source, 'version': self.content_version(source),
                                        'policy_version': pins[source]} for source in policy.sources]}
            text = yaml.safe_dump(envelope, allow_unicode=True, sort_keys=False)
            if len(text.encode('utf8')) > MAX_BYTES:
                fail('完整资料文件超过24 KB导入上限，请先缩减当前资料，或使用加密备份保留完整内容。')
            self.repository.audit(request.actor, 'plaintext_advanced_export', policy.resource, db=db)
            return {'text': text, 'version': item['version'], 'policy_version': policy.version, 'filename': identifier + '.yaml'}

    def graph(self, target, references):
        """Check complete source chains, retaining the core's version-pin rules."""
        policies, active, done = {}, set(), set()
        def lookup(key):
            if key not in policies:
                policies[key] = self.repository.policy(key)
            return policies[key]
        deadline, edges = time.monotonic() + 2, 0
        for root in references:
            stack = [(root, False)]
            while stack:
                key, leaving = stack.pop()
                if leaving:
                    active.remove(key)
                    done.add(key)
                    continue
                if key == target or key in active:
                    fail('引用会形成循环，不能导入。')
                if key in done:
                    continue
                if len(policies) >= 10000 or edges > 50000 or time.monotonic() > deadline:
                    fail('引用关系检查超过本次上限，请先缩小引用范围。')
                policy = lookup(key)
                if (policy is None or not policy.valid or policy.expires_at is not None
                        and policy.expires_at <= self.repository.clock()):
                    fail('引用不存在、已撤销或已过期，请重新核对。')
                if reserved(key) and not self.repository.provenance_target(key):
                    fail('历史来源注册不完整，请先检查恢复。')
                pins = dict(policy.source_versions)
                if len(pins) != len(policy.source_versions) or set(pins) != set(policy.sources):
                    fail('引用的来源版本记录不完整，请先检查恢复。')
                for source in policy.sources:
                    source_policy = lookup(source)
                    if source_policy is None or source_policy.version != pins[source]:
                        fail('引用的上游来源已发生变化，请先重新核对。')
                edges += len(policy.sources)
                active.add(key)
                stack.append((key, True))
                stack.extend((source, False) for source in policy.sources)
        return sorted((key, value.version) for key, value in policies.items())

    def checked(self, request, kind, identifier, envelope, expected):
        item, policy = self.target(request, kind, identifier)
        if item['version'] != expected:
            fail('资料在预览后已变化，请重新读取并预览。')
        if (set(envelope) != {'format', 'kind', 'record_version', 'policy_version', 'data', 'references'}
                or envelope['format'] != FORMAT or envelope['kind'] != kind
                or envelope['record_version'] != expected or envelope['policy_version'] != policy.version
                or not isinstance(envelope['data'], dict)):
            fail('请选择当前条目导出的高级资料文件，类型和原版本必须一致。')
        data = envelope['data']
        self.store.profile.validate(kind, data)
        if data.get('fact') or any(data.get(key) != item['data'].get(key) for key in ('id', 'schema_version', 'locked')):
            fail('文件不能更换条目标识、结构版本、锁定状态或覆盖个人事实。')
        references = envelope['references']
        if not isinstance(references, list) or len(references) > MAX_REFS:
            fail('真实引用必须为列表，每次最多60项。')
        old_pins, refs = dict(policy.source_versions), {}
        for ref in references:
            if (not isinstance(ref, dict) or set(ref) != {'resource', 'version', 'policy_version'}
                    or not isinstance(ref['resource'], str) or not 1 <= len(ref['resource']) <= 240
                    or not isinstance(ref['policy_version'], str)
                    or ref['version'] is not None and not isinstance(ref['version'], str)):
                fail('真实引用的格式无效。')
            resource = ref['resource']
            if resource in refs or resource == policy.resource:
                fail('引用不能重复或指向当前条目。')
            source_policy = self.repository.policy(resource)
            version = self.content_version(resource)
            if source_policy is None or source_policy.version != ref['policy_version'] or version != ref['version']:
                fail('引用记录或版本不匹配，请重新读取后预览。')
            if version is None and old_pins.get(resource) != ref['policy_version']:
                fail('不能新增或替换无法核对正文版本的内部来源，只能原样保留已有来源。')
            refs[resource] = ref
        for ref in data.get('memory_refs', []):
            actual = refs.get('memories/' + ref['id'])
            if actual is None or actual['version'] != ref['version']:
                fail('日记中的记忆引用必须与真实引用清单及版本一致。')
        for identifier in data.get('disagrees_with', []):
            if 'knowledge/' + identifier not in refs:
                fail('不同观点关联必须在真实引用清单中列出对应知识记录。')
        graph = self.graph(policy.resource, refs)
        affected = impact(self.store, policy.resource)
        snapshot = fingerprint({'version': item['version'], 'policy': policy.version,
                                'references': references, 'graph': graph, 'impact': affected['fingerprint']})
        return item, policy, refs, affected, snapshot

    def preview(self, request, kind, identifier, text, expected_version):
        self.manager.require(request)
        envelope = parse(text)
        with self.store.lock, self.store.db():
            item, policy, refs, affected, snapshot = self.checked(request, kind, identifier, envelope, expected_version)
            changes = []
            for field in sorted(set(item['data']) | set(envelope['data'])):
                before, after = item['data'].get(field), envelope['data'].get(field)
                if fingerprint(before) != fingerprint(after):
                    def display(value):
                        rendered = json.dumps(value, ensure_ascii=False)
                        return {'text': rendered[:240], 'truncated': len(rendered) > 240}
                    changes.append({'field': field, 'before': display(before), 'after': display(after)})
            old = dict(policy.source_versions)
            added = sorted(set(refs) - set(old))
            removed = sorted(set(old) - set(refs))
            updated = sorted(key for key in refs if key in old and old[key] != refs[key]['policy_version'])
            token = secrets.token_urlsafe(24)
            full_changes = '\n\n'.join(field + '\n修改前：\n' + json.dumps(item['data'].get(field), ensure_ascii=False, indent=2)
                + '\n修改后：\n' + json.dumps(envelope['data'].get(field), ensure_ascii=False, indent=2)
                for field in sorted(set(item['data']) | set(envelope['data']))
                if fingerprint(item['data'].get(field)) != fingerprint(envelope['data'].get(field)))
            if added or removed or updated:
                full_changes += '\n\n真实引用及版本\n修改前（原来源权限版本）：\n' + json.dumps(old, ensure_ascii=False, indent=2)
                full_changes += '\n修改后（已核对正文及权限版本）：\n' + json.dumps(list(refs.values()), ensure_ascii=False, indent=2)
            self.previews = {token: {'session': request.session_id, 'expires': time.monotonic() + 600,
                'kind': kind, 'identifier': identifier, 'expected': expected_version,
                'envelope': deepcopy(envelope), 'snapshot': snapshot,
                'full_changes': full_changes,
                'changed': bool(changes or added or removed or updated)}}
            return {'token': token, 'changes': changes, 'references': {'added': added, 'removed': removed, 'updated': updated},
                    'impact': affected, 'changed': self.previews[token]['changed'], 'written': False,
                    'notice': '保存后保留旧版本；相关派生资料需重新检查。来源链变化可能影响资料可用性，共享权限设置保持不变。'}

    def changes(self, request, token, offset):
        preview = self.entry(request, token)
        if type(offset) is not int or not 0 <= offset <= 1000000:
            fail('差异页码无效。')
        text = preview['full_changes']
        end = min(offset + 8000, len(text))
        return {'text': text[offset:end], 'next': end if end < len(text) else None}

    def entry(self, request, token):
        self.manager.require(request)
        item = self.previews.get(token)
        if not item or item['session'] != request.session_id or time.monotonic() >= item['expires']:
            fail('导入预览已过期或会话已改变，请重新选择文件预览。')
        return item

    def cancel(self, request, token):
        self.entry(request, token)
        self.previews.pop(token)
        return {'cancelled': True}

    def commit(self, request, token):
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            preview = self.entry(request, token)
            item, policy, refs, _, snapshot = self.checked(request, preview['kind'], preview['identifier'],
                                                          preview['envelope'], preview['expected'])
            if snapshot != preview['snapshot']:
                fail('资料、引用或影响范围在预览后已变化，请重新预览。')
            if not preview['changed']:
                return {'version': item['version'], 'changed': False}
            value = self.manager.correct(request, preview['kind'], preview['identifier'],
                                         preview['envelope']['data'], preview['expected'])
            current = self.repository.policy(policy.resource)
            updated = replace(current, version=secrets.token_hex(16), sources=tuple(refs),
                source_versions=tuple((key, ref['policy_version']) for key, ref in refs.items()))
            self.repository.set_policy(updated,
                current.version, request.actor, db=db)
            def archive(value):
                return {**asdict(value), 'owners': sorted(value.owners)}
            self.store.set_state('record-import-history:' + token, {'resource': policy.resource,
                'before': archive(policy), 'after': archive(updated), 'content_before': item['version'],
                'content_after': value['version'], 'actor': request.actor}, db)
            self.repository.audit(request.actor, 'advanced_import', policy.resource, item['version'], value['version'], db=db)
            return {'version': value['version'], 'changed': True}
