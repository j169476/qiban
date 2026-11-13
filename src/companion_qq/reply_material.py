"""D25/D26 reply material preparation, before any platform submission."""
import base64
import hashlib

from companion_core.errors import AssistantError
from companion_core.file_process import synthesize_audio

# Only preparation failures may change the delivery format. Authorization,
# account changes and cancellation must propagate to the task coordinator.
FALLBACK_ERRORS = frozenset({'audio_unavailable', 'audio_process_failed', 'audio_process_timeout',
                            'audio_synthesis', 'audio_encode', 'audio_duration'})


def prepare_reply(arguments, *, voice_enabled, max_audio_ms, guard, synthesizer=None):
    guard()
    text = arguments.get('text')
    mode = arguments.get('format', 'text')
    if (set(arguments)-{'text', 'format'} or not isinstance(text, str)
            or not text.strip() or len(text)>1000 or mode not in ('text','voice')):
        raise AssistantError('qq_text_invalid', '回复内容或形式无效。')
    if mode=='text':
        return {'format':'text','text':text,'fallback':None}
    if not voice_enabled:
        raise AssistantError('qq_voice_disabled', '语音回复未开启。')
    if type(max_audio_ms) is not int or not 1<=max_audio_ms<=600000:
        raise AssistantError('qq_voice_limit_invalid', '语音回复时长设置无效。')
    try:
        result=(synthesizer or synthesize_audio)(text,max_audio_ms=max_audio_ms,guard=guard)
        guard()
        try:
            audio=base64.b64decode(result['audio'],validate=True)
            duration=result['actual_audio_ms']
            if (result.get('format')!='SILK' or not audio.startswith(b'\x02#!SILK_V3')
                    or len(audio)>1024*1024 or type(duration) is not int or not 1<=duration<=max_audio_ms):
                raise ValueError()
        except (KeyError,TypeError,ValueError):
            raise AssistantError('audio_encode','语音转换结果无效。') from None
    except AssistantError as error:
        if error.code not in FALLBACK_ERRORS:
            raise
        # The coordinator must re-check this guard again at actual submission.
        guard()
        return {'format':'text','text':text,'fallback':error.code}
    return {'format':'voice','text':text,'fallback':None,'audio':audio,
            'audio_ms':duration,'sha256':hashlib.sha256(audio).hexdigest()}
