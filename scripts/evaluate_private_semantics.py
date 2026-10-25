"""Opt-in real isolated Codex cumulative-summary evaluation on synthetic conversations only."""
# ruff: noqa: E402
import argparse
import asyncio
import json
import secrets
import sys
import tempfile
import uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from companion_codex.model import CodexModel
from companion_core import conversations
from companion_core.contracts import ActorRef, Event
from companion_core.governance import GovernedContext
from companion_core.postgres import PostgresStore
from companion_core.schema import obj
from tests_postgres.test_private_compression import advance, prepare, seed

PHASES = [
    ['我不喜欢咖啡，我喜欢乌龙茶。请不要把不喜欢记成喜欢。',
     '我们约好周四折蓝色纸船。', '林川喜欢苹果，林传喜欢梨，这两个人的名字只差一个字。',
     '我在 2025 年住在成都。'],
    ['更正一下，折蓝色纸船改为周五，周四的约定取消。', '现在是 2026 年，我已经搬到苏州，成都是以前住的城市。',
     '林川还是喜欢苹果，林传还是喜欢梨，不要交换。'],
    ['我还没有决定给猫取什么名字，暂时不要记名字。', '确认：现在喜欢的茶仍然是乌龙茶，咖啡仍然不喜欢。'],
]
QUESTIONS = {
    'negation': '我不喜欢哪一种饮品？只写饮品名。',
    'correction': '折纸船约定在星期几？只写周几。',
    'identity_a': '林川喜欢什么水果？只写水果名。',
    'identity_b': '林传喜欢什么水果？只写水果名。',
    'temporal_now': '我现在住的城市是哪里？只写城市名。',
    'temporal_old': '2025 年我住的城市是哪里？只写城市名。',
    'cumulative': '最早约好折什么颜色的纸船？只写颜色。',
    'uncertain': '猫的名字确定了吗？未确定则回答未确定。',
}
EXPECTED = dict(negation='咖啡', correction='周五', identity_a='苹果', identity_b='梨', temporal_now='苏州',
                temporal_old='成都', cumulative='蓝色', uncertain='未确定')


class ObservedModel(CodexModel):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.last_error = None

    async def generate(self, *args, **kwargs):
        self.calls += 1
        try:
            return await super().generate(*args, **kwargs)
        except Exception as exc:
            self.last_error = getattr(exc, 'code', type(exc).__name__)
            raise


async def evaluate(store, output):
    service, _, token, admin, now = prepare(store, conversation_context_turns=4)
    model = ObservedModel()
    service.core.decisions.model = model
    baseline = store.version('persona')
    report = {'synthetic_only': True, 'real_isolated_codex': True, 'production_touched': False,
              'summaries': [], 'cases': [], 'passed': False}
    cursor = 0
    try:
        for index, phase in enumerate(PHASES):
            for text in [*phase, *[f'第 {index+1} 阶段无关闲聊 {i}：今天看看云，没有新增约定。' for i in range(16-len(phase))]]:
                seed(service, 1, start=cursor, content=text, speaker='user')
                cursor += 1
            for _ in range(5):
                advance(now)
                outcome = await service.maintain_local()
                if outcome is None:
                    break
                if outcome['status'] != 'saved':
                    report['failure'] = {'outcome': outcome, 'model_error': model.last_error, 'model_calls': model.calls}
                    raise RuntimeError('summary_failed:' + outcome.get('error', 'unknown'))
                value = store.read('conversation_summaries', outcome['saved'])['data']
                report['summaries'].append({'phase': index+1, 'text': value['content'], 'id': value['id']})
                print(json.dumps({'phase': index+1, 'saved_summaries': len(report['summaries'])}), flush=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        actor = ActorRef(**store.state('web-owner')['actor'])
        event = Event('local', service.local.account, 'quality-question', actor, actor.key, now[0].isoformat(), 'recall check')
        governed = GovernedContext(store, event)
        tid = conversations.thread_id(event.account, event.platform, event.thread)
        material = governed.conversation(event.account, tid)
        governed.freeze()
        assert material['summary'] and len(material['turns']) <= 4
        response = await service.core.model('只根据给定的会话摘要与最近消息回答 questions；未知就写未确定。'
            '保留否定、更正、时间与人物区别。只输出 JSON。', {'conversation': material, 'questions': QUESTIONS},
            obj({key: {'type': 'string', 'maxLength': 50} for key in QUESTIONS}), guard=governed.check)
        report['cases'] = [{'id': key, 'question': QUESTIONS[key], 'answer': response[key], 'expected': expected,
                            'passed': response[key].strip().rstrip('。.') == expected} for key, expected in EXPECTED.items()]
        report['passed'] = all(item['passed'] for item in report['cases'])
        report.update(real_model_calls=model.calls, synthetic_turns=cursor, summaries_saved=len(report['summaries']),
                      recent_turns_supplied=len(material['turns']), baseline_unchanged=store.version('persona') == baseline)
    finally:
        service._pause('synthetic_semantics_complete')
        report['role_paused_after'] = service.status()['status'] == 'paused'
        report['real_platform_actions'] = 0
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in {'summaries','cases'}}), flush=True)
    return report['passed']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    options = json.loads(args.database.read_text(encoding='utf-8'))
    assert options['dbname'] == 'companion_test' and options['host'] == '127.0.0.1'
    schema = 'semantic_' + uuid.uuid4().hex
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(**options, autocommit=True) as db:
        db.execute('CREATE SCHEMA "' + schema + '"')
    try:
        with tempfile.TemporaryDirectory(prefix='companion-semantics-') as folder:
            store = PostgresStore(folder, {**options, 'options':'-c search_path='+schema}, secrets.token_bytes(32))
            try:
                store.migrate()
                store.initialize()
                return 0 if asyncio.run(evaluate(store, args.output)) else 1
            finally:
                store.database.close()
    finally:
        with psycopg.connect(**options, autocommit=True) as db:
            db.execute('DROP SCHEMA "' + schema + '" CASCADE')


if __name__ == '__main__':
    raise SystemExit(main())
