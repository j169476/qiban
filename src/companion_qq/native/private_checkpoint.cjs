'use strict';
// SPDX-License-Identifier: MIT
// Private sequence/msgId are pagination locators only. Rebind using the
// observed client sequence and time, then verify both author identities/random.
const numeric = value => typeof value === 'string' && /^[0-9]+$/.test(value);
function validateParty(party) {
  if (!party || !numeric(party.uin) || party.uin === '0' ||
      typeof party.uid !== 'string' || !party.uid.startsWith('u_')) throw Error('Private party identity required');
}
function privateCheckpointOf(account, peer, message) {
  validateParty(account); validateParty(peer);
  if (account.uin === peer.uin || account.uid === peer.uid) throw Error('Private peer equals account');
  const author = String(message?.senderUin) === account.uin ? account : peer;
  if (message?.chatType !== 1 || message.peerUid !== peer.uid ||
      String(message.senderUin) !== author.uin || message.senderUid !== author.uid ||
      !numeric(message.clientSeq) || !numeric(message.msgRandom) ||
      !numeric(message.msgTime) || message.msgTime === '0') throw Error('Incomplete private checkpoint identity');
  return {accountUin: account.uin, accountUid: account.uid, peerUin: peer.uin, peerUid: peer.uid,
    senderUin: author.uin, senderUid: author.uid, clientSeq: message.clientSeq,
    time: message.msgTime, random: message.msgRandom};
}
async function resolvePrivateCheckpoint(checkpoint, {account, peer, query, assertCurrent, allowMissing = false}) {
  validateParty(account); validateParty(peer);
  if (!checkpoint || checkpoint.accountUin !== account.uin || checkpoint.accountUid !== account.uid ||
      checkpoint.peerUin !== peer.uin || checkpoint.peerUid !== peer.uid) throw Error('Private checkpoint account or peer changed');
  if (!numeric(checkpoint.clientSeq) || !numeric(checkpoint.time) || !numeric(checkpoint.random)) throw Error('Invalid private checkpoint');
  assertCurrent();
  const result = await query(checkpoint.clientSeq, checkpoint.time);
  assertCurrent();
  if (result?.result !== 0 || !Array.isArray(result.msgList)) throw Error('Private checkpoint lookup failed');
  if (result.msgList.some(m => m.chatType !== 1 || m.peerUid !== peer.uid)) throw Error('Private checkpoint changed conversation');
  if (allowMissing && result.msgList.length === 0) return null;
  if (result.msgList.length !== 1) throw Error('Private checkpoint not uniquely resolved');
  const message = result.msgList[0], resolved = privateCheckpointOf(account, peer, message);
  if (Object.keys(resolved).some(k => resolved[k] !== checkpoint[k])) throw Error('Private checkpoint source identity changed');
  if (!numeric(message.msgId) || message.msgId === '0' || !numeric(message.msgSeq) || message.msgSeq === '0') throw Error('Private checkpoint lacks current local locator');
  return message;
}
module.exports = {privateCheckpointOf, resolvePrivateCheckpoint};
