from copy import deepcopy

import pytest

from companion_core.context_budget import measure, pack
from companion_core.decision import DecisionService

LIMITS = {'reply_context_chars': 8000, 'reply_context_tokens': 16000, 'reply_output_reserve': 512}


def document(text, **changes):
    return {'id': 'attachment', 'event_id': 'event', 'source_version': 'version', 'kind': 'file',
            'status': 'ok', 'scope': 'body_text', 'truncated': False,
            'units': [{'source': {'page': 1}, 'text': text}], **changes}


def context(documents):
    return {'persona': {'rules': '保留基础人设' * 100}, 'event': {'text': '阅读这个文件'},
            'event_material': {'attachment_texts': documents}}


@pytest.mark.parametrize('text', ['正文' * 50000, '\n"\\😀' * 25000], ids=['chinese', 'escaped_unicode'])
def test_long_body_keeps_exact_source_prefix_and_total_budget(text):
    original = context([document(text)])
    saved = deepcopy(original)
    packed, report = pack(original, LIMITS, '规则', {'type': 'object'})
    item = packed['event_material']['attachment_text']['items'][0]
    fragment = item['units'][0]
    assert fragment['text'] == text[fragment['start']:fragment['end']]
    assert fragment['source'] == {'page': 1}
    assert item['provided_chars'] > 0 and item['omitted_extracted_chars'] > 0
    assert item['provided_chars'] + item['omitted_extracted_chars'] == len(text)
    assert item['truncated'] and report['limited']
    assert report['after']['chars'] <= 8000
    assert report['after']['estimated_tokens'] + 512 <= 16000
    assert packed['persona'] == original['persona'] and original == saved
    assert 'attachment_texts' not in packed['event_material']


def test_parser_partial_is_never_promoted_to_complete_reading():
    packed, _ = pack(context([document('仅前一页', status='truncated', truncated=True)]), LIMITS)
    item = packed['event_material']['attachment_text']['items'][0]
    assert item['omitted_extracted_chars'] == 0
    assert item['parser_incomplete'] and item['truncated'] and item['reason'] == 'parser_incomplete'


def test_multiple_units_keep_positions_and_no_source_mutation():
    doc = document('', units=[{'source': {'line': 1}, 'text': '甲'}, {'source': {'line': 2}, 'text': '乙'}])
    packed, report = pack(context([doc]), LIMITS)
    result = packed['event_material']['attachment_text']
    assert [u['source'] for u in result['items'][0]['units']] == [{'line': 1}, {'line': 2}]
    assert result['provided'] == 1 and result['omitted'] == 0 and not result['truncated']
    assert not report['limited']


def test_attachments_do_not_displace_already_selected_memory():
    base = context([])
    base['memory'] = {'records': [{'kind': 'memories', 'data': {'id': 'm', 'content': '记忆' * 1500}}], 'catalog': []}
    base['event_material']['attachment_texts'] = [document('附件' * 50000)]
    packed, _ = pack(base, LIMITS)
    assert packed['memory']['records'] == base['memory']['records']
    assert packed['event_material']['attachment_text']['truncated']


async def test_decision_never_sends_attachment_bodies_to_chat_summary_or_adds_calls():
    calls = []

    class Model:
        async def generate(self, instructions, value, schema, images, **options):
            calls.append(value)
            return {'note': '', 'action': None, 'memories': [], 'identity_proposals': [],
                    'relationship': None, 'growth_update': None}

    async def prepare(value, instructions, schema):
        assert 'attachment_texts' not in value['event_material']
        assert '全文秘密' not in str(value)
        assert value['event_material']['attachment_text']['omitted'] == 1
        return value, {'coverage': {}, 'sources': [], 'summary_calls': 0, 'summaries_reused': 0}

    await DecisionService(Model()).decide(context([document('全文秘密' * 25000)]), [],
        context_policy={'reply_context_chars': 20000, 'reply_context_tokens': 50000, 'reply_output_reserve': 512},
        prepare_context=prepare)
    assert len(calls) == 1
    assert measure(calls[0])['chars'] < 20000
    assert calls[0]['event_material']['attachment_text']['truncated']


def test_no_excerpt_space_keeps_text_reply_and_accurate_unread_reason():
    from companion_core.media_text import install_notice

    original = context([document('附件' * 50000)])
    original['event_material']['media_budget'] = {'read': 0, 'unread': 1,
        'reasons': [{'code': 'document_context_pending', 'count': 1}]}
    skeleton = deepcopy(original)
    documents = skeleton['event_material'].pop('attachment_texts')
    install_notice(skeleton, documents)
    skeleton['context_selection'] = {'limited': True, 'guidance': '未提供的资料不能当作不存在；仅依据本轮可见证据回答。'}
    size = measure(skeleton)['chars']
    packed, _ = pack(original, {**LIMITS, 'reply_context_chars': size + 10})
    material = packed['event_material']
    assert material['attachment_text']['provided'] == 0 and material['attachments_read'] == 0
    assert material['media_budget']['unread'] == 1
    assert material['media_budget']['reasons'][0]['code'] == 'text_context_limit'
    assert packed['event'] == original['event']


def test_file_excerpts_keep_image_counts_and_sources():
    value = context([document('正文')])
    value['event_material'].update(images_read=3, image_attachments_read=1,
        image_sources=[{'id': 'animation', 'frames': [0, 2, 4]}],
        media_budget={'read': 1, 'unread': 1, 'reasons': [{'code': 'document_context_pending', 'count': 1}]})
    packed, _ = pack(value, LIMITS)
    material = packed['event_material']
    assert material['images_read'] == 3 and material['attachments_read'] == 2
    assert material['media_budget']['read'] == 2 and material['media_budget']['unread'] == 0
    assert material['image_sources'] == value['event_material']['image_sources']
