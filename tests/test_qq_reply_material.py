import base64

import pytest

from companion_core.errors import AssistantError
from companion_qq.reply_material import FALLBACK_ERRORS, prepare_reply


def test_text_choice_never_starts_synthesis():
    def forbidden(*args,**kwargs):
        pytest.fail('text reply must not synthesize')
    result=prepare_reply({'text':'原回复','format':'text'},voice_enabled=True,max_audio_ms=1000,
                         guard=lambda:None,synthesizer=forbidden)
    assert result=={'format':'text','text':'原回复','fallback':None}


@pytest.mark.parametrize('code',sorted(FALLBACK_ERRORS))
def test_known_preparation_failure_preserves_exact_text_without_second_generation(code):
    calls=[]
    def failed(text,**kwargs):
        calls.append(text)
        raise AssistantError(code,'failed')
    original='这是原回复🙂，不要重新生成。'
    result=prepare_reply({'text':original,'format':'voice'},voice_enabled=True,max_audio_ms=1000,
                         guard=lambda:None,synthesizer=failed)
    assert result=={'format':'text','text':original,'fallback':code}
    assert calls==[original]


@pytest.mark.parametrize('code',['reply_not_authorized','account_changed','media_cancelled','qq_send_unconfirmed'])
def test_cancellation_authority_and_submission_errors_never_fall_back(code):
    def failed(*args,**kwargs):
        raise AssistantError(code,'stop')
    with pytest.raises(AssistantError) as error:
        prepare_reply({'text':'原回复','format':'voice'},voice_enabled=True,max_audio_ms=1000,
                      guard=lambda:None,synthesizer=failed)
    assert error.value.code==code


def test_revocation_during_failed_synthesis_blocks_text_fallback():
    active=True
    def guard():
        if not active:
            raise AssistantError('account_changed','stop')
    def failed(*args,**kwargs):
        nonlocal active
        active=False
        raise AssistantError('audio_process_timeout','failed')
    with pytest.raises(AssistantError) as error:
        prepare_reply({'text':'原回复','format':'voice'},voice_enabled=True,max_audio_ms=1000,
                      guard=guard,synthesizer=failed)
    assert error.value.code=='account_changed'


def test_voice_result_keeps_original_text_and_verified_encoded_duration():
    audio=b'\x02#!SILK_V3'+b'fixture'
    def synthesize(*args,**kwargs):
        return {'audio':base64.b64encode(audio).decode(),'actual_audio_ms':980,'format':'SILK'}
    result=prepare_reply({'text':'原回复','format':'voice'},voice_enabled=True,max_audio_ms=1000,
                         guard=lambda:None,synthesizer=synthesize)
    assert result['audio']==audio and result['audio_ms']==980 and result['text']=='原回复'
    assert result['format']=='voice' and result['fallback'] is None


def test_voice_disabled_rejects_before_synthesis():
    def forbidden(*args,**kwargs):
        pytest.fail('disabled voice must not synthesize')
    with pytest.raises(AssistantError) as error:
        prepare_reply({'text':'原回复','format':'voice'},voice_enabled=False,max_audio_ms=1000,
                      guard=lambda:None,synthesizer=forbidden)
    assert error.value.code=='qq_voice_disabled'
