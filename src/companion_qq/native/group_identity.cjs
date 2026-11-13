'use strict';
// SPDX-License-Identifier: MIT
// Qiban-owned checks derived from the fixed Windows runtime's test-group evidence.
// Call only for events from an account-verified native session, never untrusted JSON.
class ConfirmedGroupMessages {
  constructor(account, group) {
    if (!account?.uin || !account?.uid || !group) throw Error('Verified account and group required');
    this.account = {...account}; this.group = String(group); this.messages = new Map();
  }
  inGroup(message) { return message?.chatType === 2 && message.peerUid === this.group; }
  isSelf(uin, uid) { return String(uin) === String(this.account.uin) && uid === this.account.uid; }
  // Feed only own-send status callbacks, not getSourceOfReplyMsg records.
  observeSent(message) {
    if (!this.inGroup(message) || message.sendStatus !== 2 || !this.isSelf(message.senderUin, message.senderUid)) return false;
    if (!/^[1-9][0-9]*$/.test(message.msgSeq) || !/^[1-9][0-9]*$/.test(message.msgId)) throw Error('Final send identity missing');
    const old = this.messages.get(message.msgSeq);
    if (old && old !== message.msgId) throw Error('Conflicting final group sequence');
    this.messages.set(message.msgSeq, message.msgId);
    return true;
  }
  classify(message) {
    if (!this.inGroup(message) || this.isSelf(message.senderUin, message.senderUid)) return 'none';
    let trigger = 'none';
    for (const element of message.elements || []) {
      const text = element.elementType === 1 ? element.textElement : null;
      if (text?.atType === 2 && this.isSelf(text.atUid, text.atNtUid)) trigger = 'mention';
      const reply = element.elementType === 7 ? element.replyElement : null;
      if (reply && this.isSelf(reply.senderUid, reply.senderUidStr) && this.messages.has(reply.replayMsgSeq)) return 'reply';
    }
    return trigger;
  }
}
module.exports = {ConfirmedGroupMessages};
