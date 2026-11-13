'use strict';
// SPDX-License-Identifier: MIT
// Native msgId is a cache-local locator. Group sequence + author/time binds a checkpoint.
const {isGroupSystemRecord} = require('./group_records.cjs');
function checkpointOf(account, group, message) {
  if (!account || message?.chatType !== 2 || message.peerUid !== group ||
      typeof message.msgSeq !== 'string' || !/^[1-9][0-9]*$/.test(message.msgSeq) ||
      typeof message.senderUid !== 'string' || !message.senderUid.startsWith('u_') ||
      !/^[1-9][0-9]*$/.test(String(message.senderUin)) || !/^[1-9][0-9]*$/.test(String(message.msgTime))) throw Error('Incomplete group checkpoint identity');
  return {account: String(account), group, seq: message.msgSeq, senderUin: String(message.senderUin), senderUid: message.senderUid, time: String(message.msgTime)};
}
async function resolveCheckpoint(checkpoint, {account, group, query, assertCurrent, allowMissing = false}) {
  if (checkpoint.account !== String(account) || checkpoint.group !== group) throw Error('Checkpoint account or group changed');
  assertCurrent();
  const result = await query(checkpoint.seq);
  assertCurrent();
  if (result?.result !== 0 || !Array.isArray(result.msgList)) throw Error('Checkpoint lookup failed');
  const matches = result.msgList.filter(m => m.chatType === 2 && m.peerUid === group && m.msgSeq === checkpoint.seq && !isGroupSystemRecord(m));
  if (allowMissing && matches.length === 0) return null;
  if (matches.length !== 1) throw Error('Checkpoint not uniquely resolved');
  const resolved = checkpointOf(account, group, matches[0]);
  for (const key of ['account', 'group', 'seq', 'senderUin', 'senderUid', 'time']) {
    if (resolved[key] !== checkpoint[key]) throw Error('Checkpoint author or time changed');
  }
  if (typeof matches[0].msgId !== 'string' || !/^[1-9][0-9]*$/.test(matches[0].msgId)) throw Error('Checkpoint lacks current local locator');
  return matches[0];
}
module.exports = {checkpointOf, resolveCheckpoint};
