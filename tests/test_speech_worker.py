import io
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip('numpy')
pytest.importorskip('av')
from companion_core.errors import AssistantError
from companion_core.file_process import parse_audio
from companion_core.speech_worker import decode, transcribe


def wav(seconds=1):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(b'\0\0'*round(24000*seconds))
    return stream.getvalue()


def test_decoded_duration_and_exact_remaining_limit():
    samples=decode(wav(),1000,Path('.'))
    assert len(samples)==16000
    with pytest.raises(ValueError,match='audio_duration'):
        decode(wav(),999,Path('.'))


def test_unsupported_and_empty_audio_are_not_transcripts():
    with pytest.raises(ValueError,match='audio_format'):
        decode(b'fake recording',1000,Path('.'))
    with pytest.raises(ValueError,match='audio_decode'):
        decode(wav(0),1000,Path('.'))


def test_transcript_keeps_time_positions_and_offline_cpu_configuration(monkeypatch):
    calls=[]
    class Engine:
        def __init__(self,path,**options):
            assert options['local_files_only'] and options['device']=='cpu'
            calls.append(options)
        def transcribe(self,samples,**options):
            assert len(samples)==16000 and options['condition_on_previous_text'] is False
            return iter([SimpleNamespace(start=.1,end=.8,text='中文内容')]),None
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(WhisperModel=Engine))
    result=transcribe(wav(),1000,Path('.'))
    assert result['units']==[{'source':{'start_ms':100,'end_ms':800},'text':'中文内容'}]
    assert result['actual_audio_ms']==1000 and 'may_contain_errors' in result['scope']
    with pytest.raises(ValueError,match='audio_duration'):
        transcribe(wav(2),1000,Path('.'))
    assert len(calls)==1  # Over-duration audio never starts inference.


def test_speech_timeout_and_cancellation_are_explicit():
    with pytest.raises(AssistantError) as error:
        parse_audio(wav(),max_audio_ms=1000,guard=lambda:None,timeout=.000001)
    assert error.value.code=='audio_process_timeout'
    calls=[]
    def guard():
        calls.append(1)
        if len(calls)>1:
            raise AssistantError('test_cancel','cancel')
    with pytest.raises(AssistantError) as error:
        parse_audio(wav(),max_audio_ms=1000,guard=guard)
    assert error.value.code=='test_cancel'


def test_synthesis_timeout_and_cancellation_do_not_publish_partial_audio():
    from companion_core.file_process import synthesize_audio

    with pytest.raises(AssistantError) as error:
        synthesize_audio('测试内容',max_audio_ms=120000,guard=lambda:None,timeout=.000001)
    assert error.value.code=='audio_process_timeout'
    calls=[]
    def guard():
        calls.append(1)
        if len(calls)>1:
            raise AssistantError('test_cancel','cancel')
    with pytest.raises(AssistantError) as error:
        synthesize_audio('测试内容',max_audio_ms=120000,guard=guard)
    assert error.value.code=='test_cancel'
