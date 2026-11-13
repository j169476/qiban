'use strict';
// SPDX-License-Identifier: MIT
const {privateCheckpointOf} = require('./private_checkpoint.cjs');
// Freeze the latest observed local row after sync. This does not assert that
// arbitrary server history is complete; the window reader reports its own gaps.
async function capturePrivateHead({account, peer, gate, service, guard, signal, allowEmpty = false}) {
  guard();
  const generation = await gate.wait({signal});
  const check = () => {signal?.throwIfAborted(); gate.assertCurrent(generation); guard();};
  check();
  const result = await service.getLatestDbMsgs({chatType: 1, peerUid: peer.uid, guildId: ''}, 20);
  check();
  if (allowEmpty && result?.result === 0 && Array.isArray(result.msgList) && !result.msgList.length) return null;
  if (result?.result !== 0 || !Array.isArray(result.msgList) ||
      result.msgList.length === 0 || result.msgList.length > 20) throw Error('Private head snapshot unavailable');
  let head = null, sequence = -1n;
  for (const message of result.msgList) {
    const checkpoint = privateCheckpointOf(account, peer, message);
    if (typeof message.msgSeq !== 'string' || !/^[1-9][0-9]*$/.test(message.msgSeq)) throw Error('Invalid private head locator');
    const current = BigInt(message.msgSeq);
    if (current > sequence) {head = checkpoint; sequence = current;}
    else if (current === sequence && JSON.stringify(head) !== JSON.stringify(checkpoint)) throw Error('Private head source identity conflict');
  }
  return head;
}
module.exports = {capturePrivateHead};
