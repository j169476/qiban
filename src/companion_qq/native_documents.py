"""Source-bound QQ documents; local parsing is not a model call."""
import base64
from collections import Counter

from companion_core import events
from companion_core.contracts import Event
from companion_core.errors import AssistantError
from companion_core.file_process import parse_audio, parse_file, parse_image
from companion_core.media_budget import REASONS, TurnMediaBudget
from companion_core.media_originals import MediaOriginals
from companion_core.reply_authorization import ReplyAuthorizations

FAILURES = {
    'media_capacity_full': '附件原件空间不足，受保护原件不能自动回收',
    'media_expired': '附件原件已清理，本轮不自动重新下载',
    'qq_media_limit': '附件超过本轮允许的大小',
    'qq_media_source_unavailable': '当前无法取得原消息附件',
    'qq_media_download_failed': '附件下载失败',
    'qq_media_timeout': '附件下载超时',
    'audio_unavailable': '本地语音资源尚未安装',
    'audio_process_failed': '本地语音处理失败',
    'audio_process_timeout': '本地语音处理超时',
    'audio_duration': '语音实际时长超过本轮剩余额度',
    'audio_decode': '语音解码失败',
    'audio_format': '语音格式尚不支持',
    'audio_transcript_limit': '语音转写超过本机处理上限',
    'image_process_failed': '图片格式、像素、帧数不支持或解码失败',
    'image_process_timeout': '图片解码超时',
    'image_frame_budget': '动图取帧已占用本轮图片额度',
    'file_process_failed': '文件解析进程失败',
    'file_process_timeout': '文件解析超时',
}


def prepare_documents(module, pending, *, owner, task, guard, parser=None):
    parser = parser or parse_file
    plan = {'limits': pending['limits'], 'selected': pending['candidates']}
    budget = TurnMediaBudget(plan)
    media = MediaOriginals(module.store)
    reasons = Counter({r['code']: r['count'] for r in pending['reasons'] if r['code'] != 'unavailable'})
    documents, images, image_sources = [], [], []
    audio_ms = 0
    for candidate in plan['selected']:
        guard()
        if candidate['kind'] not in ('file', 'image', 'audio'):
            reasons['unavailable'] += 1
            continue
        if candidate['kind'] == 'image' and len(images) >= pending['limits']['images']:
            reasons['image_frame_budget'] += 1
            continue
        lease = budget.reserve(candidate)
        if not lease['accepted']:
            reasons[lease['reason']] += 1
            continue
        try:
            original = module.provider.save_attachment(module, candidate, max_bytes=lease['max_bytes'],
                owner=owner, task=task, guard=guard)
            source = events.get(module.store, candidate['event_id'])
            event = Event.from_dict(source['event'])
            scope = module.scope(event)
            permission = ReplyAuthorizations(module.store).version(scope)

            def check():
                guard()
                module.validate_context_event(event)
                if events.get(module.store, event.id)['version'] != candidate['source_version']:
                    raise AssistantError('media_source_changed', '附件来源已改变。')
                ReplyAuthorizations(module.store).check(scope, permission)

            content = media.read(scope, permission, candidate['id'], guard=check)
            if candidate['kind'] == 'audio':
                parsed = parse_audio(content, max_audio_ms=lease['max_audio_ms'], guard=check)
            elif candidate['kind'] == 'image':
                parsed = parse_image(content, slots=min(3, pending['limits']['images']-len(images)), guard=check)
            else:
                parsed = parser(original['name'], content, guard=check)
            check()
            budget.complete(lease, actual_bytes=len(content),
                            actual_audio_ms=parsed['actual_audio_ms'] if candidate['kind']=='audio' else 0)
        except AssistantError as error:
            if error.code not in FAILURES:
                raise  # Cancellation, ownership, authorization and integrity never become an unread file.
            if budget.active is not None:
                budget.failed(lease)
            reasons[error.code] += 1
            continue
        if candidate['kind'] == 'audio':
            audio_ms += parsed['actual_audio_ms']
        if candidate['kind'] == 'image':
            positions = []
            for frame in parsed['frames']:
                images.append(base64.b64decode(frame['png'], validate=True))
                positions.append({'input_index': len(images)-1, **{key: frame[key] for key in ('index', 'width', 'height')}})
            image_sources.append({**candidate, **{key: parsed[key] for key in
                ('total_frames', 'omitted_frames', 'sampled', 'budget_limited')}, 'frames': positions})
            continue
        if not parsed['units']:
            reasons[candidate['kind'] + '_' + parsed['status']] += 1
            continue
        documents.append({**candidate, 'units': parsed['units'], 'status': parsed['status'],
                          'scope': parsed.get('scope', parsed['format']), 'truncated': parsed['truncated']})
    if documents:
        reasons['document_context_pending'] = len(documents)
    descriptions = {**REASONS, **FAILURES, 'document_context_pending': '正文已解析，尚待整体上下文选择'}
    return {'images': tuple(images), 'images_read': len(images), 'image_attachments_read': len(image_sources),
            'image_sources': image_sources, 'attachment_status': 'images_provided' if images else 'not_processed',
            'image_guidance': '图片按input_index对应输入，index为动画帧序号；仅看到列出的画面，抽帧不代表完整观看，未提供帧不能补猜。',
            'audio_guidance': '语音材料为本地自动转写，可能存在同音字、姓名、数字或否定词错误；时间段对应原语音，不代表直接听过原音。',
            'attachment_texts': documents, 'attachments_parsed': len(documents), 'attachments_read': len(image_sources),
            'media_budget': {**pending, 'read': len(image_sources), 'image_frames': len(images), 'audio_ms': audio_ms, 'audio_budget_ms': budget.used_audio_ms, 'unread': sum(reasons.values()),
                'reasons': [{'code': code, 'count': count, 'reason': descriptions.get(code, '附件未提取到可读正文：' + code)}
                            for code, count in reasons.items()]}}
