from types import SimpleNamespace

import pytest

from companion_core.context_budget import pack
from companion_core.errors import AssistantError
from companion_core.media_budget import DEFAULTS
from companion_qq.native_documents import prepare_documents


@pytest.mark.parametrize('durations,expected_limits,read,audio_ms', [
    ([70000,40000],[120000,50000],2,110000),
    ([120000],[120000],1,120000),
    (['too_long'],[120000],0,0),
])
def test_audio_shares_actual_duration_and_never_starts_extra_work_after_exhaustion(monkeypatch,durations,expected_limits,read,audio_ms):
    from companion_qq import native_documents as module

    candidates=[{'id':str(i),'event_id':str(i),'source_version':'v','kind':'audio'} for i in range(2)]
    downloaded,limits=[],[]
    class Originals:
        def __init__(self,store):
            pass
        def read(self,*args,guard):
            guard()
            return b'fixture'
    class Permissions:
        def __init__(self,store):
            pass
        def version(self,scope):
            return 'p'
        def check(self,*args):
            pass
    def save(env,candidate,**kwargs):
        downloaded.append(candidate['id'])
        return {'name':'voice.silk'}
    def recognize(content,*,max_audio_ms,guard):
        guard()
        index=len(limits)
        limits.append(max_audio_ms)
        duration=durations[index]
        if duration=='too_long':
            raise AssistantError('audio_duration','too long')
        return {'units':[{'source':{'start_ms':0,'end_ms':duration},'text':'语音内容'}],
                'status':'ok','format':'speech','scope':'local_asr_transcript_may_contain_errors',
                'truncated':False,'actual_audio_ms':duration}
    monkeypatch.setattr(module,'MediaOriginals',Originals)
    monkeypatch.setattr(module,'ReplyAuthorizations',Permissions)
    monkeypatch.setattr(module.events,'get',lambda store,key:{'version':'v','event':{'id':key}})
    monkeypatch.setattr(module,'Event',SimpleNamespace(from_dict=lambda row:SimpleNamespace(**row)))
    monkeypatch.setattr(module,'parse_audio',recognize)
    env=SimpleNamespace(store=None,scope=lambda event:None,validate_context_event=lambda event:None,
                        provider=SimpleNamespace(save_attachment=save))
    result=prepare_documents(env,{'limits':DEFAULTS,'candidates':candidates,'reasons':[],'unread':2},
                             owner='owner',task='task',guard=lambda:None)
    result.pop('images')
    final,_=pack({'event_material':result},None)
    budget=final['event_material']['media_budget']
    assert limits==expected_limits and len(downloaded)==len(expected_limits)
    assert budget['read']==read and budget['unread']==2-read and budget['audio_ms']==audio_ms
    if read<2:
        assert any(r['code']=='audio_duration' for r in budget['reasons'])
