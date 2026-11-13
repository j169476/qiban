"""Encrypted at-most-once journal around the owned native text transport."""
import base64
import hashlib
import re
from dataclasses import dataclass

from companion_core.contracts import Event
from companion_core.reply_authorization import ReplyAuthorizations, require


@dataclass(frozen=True)
class NativeSendReceipt:
    message_id: str
    reference: str | None = None


class NativeSender:
    def __init__(self, store, worker, identity):
        self.store, self.worker = store, worker
        self.identity = {"uin": identity["uin"], "uid": identity["uid"]}

    def send_action(self, action_id, scope, text):
        return self.send_reply_action(action_id,scope,{'text':text},
                                     {'format':'text','text':text,'fallback':None},guard=lambda:None)

    def send_reply_action(self, action_id, scope, arguments, material, *, guard):
        from .module import digest

        text=arguments.get('text')
        mode=material.get('format')
        require(set(arguments)<= {'text','format'} and arguments.get('format','text') in {'text','voice'}
                and material.get('text')==text and mode in {'text','voice'},'qq_action_mismatch')
        audio=None
        if mode=='voice':
            require(arguments.get('format')=='voice','qq_action_mismatch')
            audio=material.get('audio')
            require(isinstance(audio,bytes) and audio.startswith(b'\x02#!SILK_V3') and len(audio)<=1024*1024
                    and type(material.get('audio_ms')) is int and 0<material['audio_ms']<=600000
                    and hashlib.sha256(audio).hexdigest()==material.get('sha256'),'qq_voice_invalid')
        guard()

        require(scope.platform == "qq" and scope.account_identity == self.identity["uin"]
                and scope.kind in {"private", "group"}, "qq_account_mismatch")
        require(isinstance(text, str) and text.strip() and len(text) <= 1000, "qq_text_invalid")
        fingerprint = digest({"owner": self.identity, "scope": [scope.kind, scope.target_id], "text": text})
        if 'format' in arguments:
            fingerprint=digest({'request':fingerprint,'requested':arguments['format'],'format':mode,
                                'audio_sha256':material.get('sha256'),'fallback':material.get('fallback')})
        key = "qq-native-send:" + action_id
        with self.store.lock:
            previous = self.store.state(key)
            if previous is not None:
                require(previous["fingerprint"] == fingerprint, "qq_action_mismatch")
                require(previous["status"] == "confirmed", "qq_send_unconfirmed")
                return NativeSendReceipt(**previous["receipt"])
            with self.store.db() as db:
                row = db.execute("SELECT * FROM core_actions WHERE id=?", (action_id,)).fetchone()
                require(row is not None and row["status"] == "dispatching" and row["platform"] == "qq"
                        and row["account"] == self.identity["uin"] and row["capability"] == "message.reply",
                        "qq_action_missing")
                body = self.store.ledger_decode("core_actions.body", row["body"])
                event = Event.from_dict(body["event"])
                require(body["arguments"] == arguments and event.account == self.identity["uin"]
                        and event.visibility == scope.kind and event.metadata["qq"]["target_id"] == scope.target_id,
                        "qq_action_mismatch")
                require(ReplyAuthorizations(self.store).version(scope, db=db) is not None, "reply_not_authorized")
            identity = self.worker.request("identity")
            require(all(identity.get(k) == v for k, v in self.identity.items()), "qq_account_mismatch")
            guard()
            if mode=='voice':
                prepared=self.worker.request('prepare_voice',kind=scope.kind,target=scope.target_id,
                    audio=base64.b64encode(audio).decode('ascii'),audio_ms=material['audio_ms'])
            else:
                prepared = self.worker.request("prepare_text", kind=scope.kind, target=scope.target_id, text=text)
            require(isinstance(prepared, dict) and set(prepared) == {"token", "message_id"}
                    and isinstance(prepared["token"], str) and re.fullmatch(r"[a-f0-9]{64}", prepared["token"])
                    and isinstance(prepared["message_id"], str)
                    and re.fullmatch(r"[1-9][0-9]{0,19}", prepared["message_id"]), "qq_native_protocol_invalid")
            guard()
            record = {"fingerprint": fingerprint, "status": "attempted", "message_id": prepared["message_id"],
                      'format':mode,'fallback':material.get('fallback')}
            try:
                # This commit precedes submit_text. Restart never recreates this ticket.
                self.store.set_state(key, record)
            except BaseException:
                self.worker.close()  # Prepared ticket cannot outlive failed journalling.
                raise
            try:
                guard()
                receipt = self.worker.request("submit_text", token=prepared["token"])
                require(isinstance(receipt, dict) and set(receipt) == {"message_id", "reference"}
                        and receipt["message_id"] == prepared["message_id"], "qq_send_unconfirmed")
                reference = receipt["reference"]
                require((scope.kind == "private" and reference is None) or
                        (scope.kind == "group" and isinstance(reference, str)
                         and re.fullmatch(r"native-seq:[1-9][0-9]{0,19}", reference)), "qq_send_unconfirmed")
                self.store.set_state(key, {**record, "status": "confirmed", "receipt": receipt})
                return NativeSendReceipt(**receipt)
            except BaseException:
                # The durable attempted record remains uncertain; no retry or fallback.
                self.worker.close()
                raise
