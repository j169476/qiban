'use strict';
// SPDX-License-Identifier: MIT
// Capture a post-sync contact candidate; it may still lag local messages.
// Reconcile a local snapshot with advanceGroupHead before freezing the window.
const {checkpointOf} = require('./group_checkpoint.cjs');
const {isGroupSystemRecord} = require('./group_records.cjs');
function advanceGroupHead(candidate, messages) {
  let head = {...candidate};
  for (const message of messages) {
    if (message.chatType !== 2 || message.peerUid !== candidate.group) throw Error('Head snapshot changed conversation');
    if (isGroupSystemRecord(message)) continue;
    const checkpoint = checkpointOf(candidate.account, candidate.group, message);
    if (BigInt(checkpoint.seq) > BigInt(head.seq)) head = checkpoint;
    else if (checkpoint.seq === head.seq && ['senderUin', 'senderUid', 'time'].some(k => checkpoint[k] !== head[k])) throw Error('Head source identity conflict');
  }
  return head;
}
class PostSyncGroupHead {
  constructor(account, group, gate) { this.account = account; this.group = group; this.gate = gate; this.head = null; this.waiters = new Set(); }
  observe(name, payload) {
    if (this.gate.phase !== 'ready') return;
    if (this.head?.generation === this.gate.generation) return;
    let contacts = [];
    if (name === 'onRecentContactListChangedVer2' && Array.isArray(payload?.[0])) contacts = payload[0].flatMap(list => list.changedList || []);
    if (name === 'onRecentContactNotification' && Array.isArray(payload?.[0])) contacts = payload[0];
    const targets = contacts.filter(c => c.chatType === 2 && c.peerUid === this.group && c.sendStatus === 2 &&
      typeof c.msgSeq === 'string' && /^[1-9][0-9]*$/.test(c.msgSeq));
    if (!targets.length) return;
    const latest = targets.reduce((a, b) => BigInt(a.msgSeq) > BigInt(b.msgSeq) ? a : b);
    this.head = {generation: this.gate.generation, checkpoint: checkpointOf(this.account, this.group, latest)};
    for (const waiter of [...this.waiters]) waiter.resolve(this.head);
  }
  async wait({generation, timeoutMs = 45000, signal} = {}) {
    this.gate.assertCurrent(generation);
    if (signal?.aborted) throw signal.reason || Error('Group head cancelled');
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw Error('Finite group-head deadline required');
    let result = this.head?.generation === generation ? this.head : null;
    if (!result) result = await new Promise((resolve, reject) => {
      let timer;
      const finish = (fn, value) => {clearTimeout(timer); this.waiters.delete(waiter); signal?.removeEventListener('abort', abort); fn(value);};
      const waiter = {resolve: value => finish(resolve, value)};
      const abort = () => finish(reject, signal.reason || Error('Group head cancelled'));
      this.waiters.add(waiter);
      signal?.addEventListener('abort', abort, {once: true});
      timer = setTimeout(() => finish(reject, Error('No post-sync group head before deadline')), timeoutMs);
    });
    this.gate.assertCurrent(generation);
    if (result.generation !== generation) throw Error('Group head generation changed');
    return {...result.checkpoint};
  }
}
module.exports = {PostSyncGroupHead, advanceGroupHead};
