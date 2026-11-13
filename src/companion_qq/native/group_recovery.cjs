'use strict';
// SPDX-License-Identifier: MIT
// Compose the verified startup reads; the caller commits intake/receipts separately.
const {advanceGroupHead} = require('./group_head.cjs');
const {resolveCheckpoint, checkpointOf} = require('./group_checkpoint.cjs');
const {readGroupWindow} = require('./group_window.cjs');
async function queryGroupHead({account, group, contactQuery, service, assertCurrent, allowEmpty = false}) {
  const peer = {chatType: 2, peerUid: group, guildId: ''};
  assertCurrent();
  const contacts = await contactQuery([peer]); assertCurrent();
  if (allowEmpty && contacts?.result === 0 && contacts.foundContacts?.length === 0) {
    const local = await service.getLatestDbMsgs(peer, 20); assertCurrent();
    if (local?.result === 0 && Array.isArray(local.msgList) && !local.msgList.length) return null;
  }
  if (contacts?.result !== 0 || !Array.isArray(contacts.foundContacts) || contacts.foundContacts.length !== 1 ||
      !Array.isArray(contacts.unFound) || contacts.unFound.length !== 0 ||
      contacts.foundContacts[0]?.sendStatus !== 2) throw Error('Group contact snapshot unavailable');
  const candidate = checkpointOf(account, group, contacts.foundContacts[0]);
  const local = await service.getLatestDbMsgs(peer, 20); assertCurrent();
  if (local.result !== 0 || !Array.isArray(local.msgList) || local.msgList.length > 20) throw Error('Group head snapshot unavailable');
  return advanceGroupHead(candidate, local.msgList);
}
async function recoverGroupWindow({account, group, lowerCheckpoint, headGate, gate, service, guard,
  signal, pageSize = 20, maxPages = 100, upperCheckpoint = null, contactQuery = null, since = null}) {
  guard();
  const generation = await gate.wait({signal});
  const check = () => {signal?.throwIfAborted(); gate.assertCurrent(generation); guard();};
  const peer = {chatType: 2, peerUid: group, guildId: ''};
  let head = upperCheckpoint;
  if (!head) {
    if (contactQuery) {
      // Request this conversation after sync instead of depending on an optional
      // notification being emitted after the listener observes sync completion.
      head = await queryGroupHead({account, group, contactQuery, service, assertCurrent: check});
    } else {
      const candidate = await headGate.wait({generation, signal}); check();
      const local = await service.getLatestDbMsgs(peer, 20); check();
      if (local.result !== 0 || !Array.isArray(local.msgList)) throw Error('Group head snapshot unavailable');
      head = advanceGroupHead(candidate, local.msgList);
    }
  }
  const lookup = {account, group, query: seq => service.getSingleMsg(peer, seq), assertCurrent: check};
  const upper = await resolveCheckpoint(head, lookup);
  const lower = lowerCheckpoint === null && since !== null ? null : await resolveCheckpoint(lowerCheckpoint, {...lookup, allowMissing: true});
  const result = await readGroupWindow({group, since, lower: lower || (since !== null ? null : {msgId: null, msgSeq: lowerCheckpoint.seq}), upper,
    pageSize, maxPages, assertCurrent: check, query: (id, count) => service.getMsgsIncludeSelf(peer, id, count, true)});
  check();
  return {...result, candidate: head, checkpoint: checkpointOf(account, group, upper)};
}
module.exports = {recoverGroupWindow, queryGroupHead};
