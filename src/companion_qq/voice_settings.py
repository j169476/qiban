"""Explicit QQ voice opt-in and a separate outgoing duration setting."""
import secrets

from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository

KEY='qq-voice-reply-settings-v1'


class VoiceSettings:
    def __init__(self,store):
        self.store=store

    def read(self):
        return self.store.state(KEY,{'enabled':False,'seconds':30,'revision':None})

    def configure(self,enabled,seconds,expected,*,actor):
        if type(enabled) is not bool or type(seconds) is not int or not 1<=seconds<=600:
            raise AssistantError('qq_voice_settings','请选择语音开关，并输入1至600秒的整数上限。')
        with self.store.lock,self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=self.read()
            if old['revision']!=expected:
                raise AssistantError('edit_conflict','语音回复设置已改变，请重新读取。')
            value={'enabled':enabled,'seconds':seconds,'revision':secrets.token_hex(16)}
            self.store.set_state(KEY,value,db)
            GovernanceRepository(self.store).audit(actor,'qq_voice_settings_changed',KEY,old['revision'],value['revision'],db=db)
        return value
