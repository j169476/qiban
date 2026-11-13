"""One-time, resumable correction of generated learning memories (D52).

Immutable task stages remain evidence. Only the exact old generated summary is
eligible; administrator-edited text and deleted memories are never recreated.
"""
from .contracts import stable_key
from .task_learning import assessment

STATE = 'learning-memory-correction-v1'
PREFIX = '用户明确安排的任务结果（保留材料与模型自检的局限）：\n'
BATCH = 32


def _correct(store, identifier):
    memory = store.read('memories', identifier)
    data = memory['data']
    task_id = data.get('source', {}).get('reference', '')
    if (identifier != stable_key('task-memory', task_id) or data.get('kind') != 'experience'
            or 'explicit_task' not in data.get('tags', []) or data['source'].get('type') != 'interaction'
            or not store.exists('task_requests', task_id)):
        return False
    task = store.read('task_requests', task_id)['data']
    state = store.state('explicit-task:' + task_id, {})
    if task['kind'] != 'learn' or state.get('status') != 'completed' or not state.get('latest'):
        return False
    if not store.exists('task_results', state['latest']):
        return False
    result = store.read('task_results', state['latest'])['data']
    if result['task_id'] != task_id or result['phase'] != 'assessed' or not result['complete']:
        return False
    payload = result['payload']
    grades = payload.get('grades')
    if not grades:
        return False
    checked = assessment(grades)
    if (not checked['conflicts'] or result['summary'] == checked['summary']
            or data['content'] != (PREFIX + result['summary'])[:4000]):
        return False
    content = (PREFIX + '更正：原评阅同时列有通过与缺口，本次不计通过；原评阅和旧记忆版本保留。\n'
               + checked['summary'] + '\n评阅局限：' + payload.get('limitations', ''))[:4000]
    # write() retains encrypted history, changes the current version and policy
    # version together, and preserves the existing policy's scope and sources.
    store.write('memories', {**data, 'content': content}, memory['version'],
                'learning-memory-correction-v1', identifier, allow_locked=True)
    return True


def repair_batch(store):
    """Commit at most 32 candidates and their cursor together."""
    with store.lock, store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        state = store.state(STATE, {'after': '', 'scanned': 0, 'corrected': 0, 'complete': False}, db)
        if state['complete']:
            return state
        rows = db.execute("SELECT id FROM content_records WHERE kind='memories' AND id LIKE ? "
                          'AND id>? ORDER BY id LIMIT ?', ('task-memory-%', state['after'], BATCH)).fetchall()
        corrected = sum(_correct(store, row['id']) for row in rows)
        result = {'after': rows[-1]['id'] if rows else state['after'],
                  'scanned': state['scanned'] + len(rows), 'corrected': state['corrected'] + corrected,
                  'complete': len(rows) < BATCH}
        store.set_state(STATE, result, db)
        return result


def repair(store):
    """Finish before opening model admission; interrupted pages resume on startup."""
    while True:
        result = repair_batch(store)
        if result['complete']:
            return result
