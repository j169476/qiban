'use strict';
// SPDX-License-Identifier: MIT
// Both endpoints are stable source identities, never another cache's locators.
const {privateCheckpointOf, resolvePrivateCheckpoint} = require('./private_checkpoint.cjs');
const {readPrivateWindow} = require('./private_window.cjs');
async function recoverPrivateWindow({account, peer, lowerCheckpoint, upperCheckpoint,
  gate, service, guard, signal, pageSize = 100, maxPages = 100, since = null}) {
  guard();
  const generation = await gate.wait({signal});
  const check = () => {signal?.throwIfAborted(); gate.assertCurrent(generation); guard();};
  const endpoint = {chatType: 1, peerUid: peer.uid, guildId: ''};
  const lookup = {account, peer, assertCurrent: check,
    query: (seq, time) => service.getMsgByClientSeqAndTime(endpoint, seq, time)};
  const upper = await resolvePrivateCheckpoint(upperCheckpoint, lookup);
  const lower = lowerCheckpoint === null && since !== null ? null : await resolvePrivateCheckpoint(lowerCheckpoint, {...lookup, allowMissing: true});
  const result = await readPrivateWindow({peerUid: peer.uid, lower, upper, assertCurrent: check,
    pageSize, maxPages, since, query: (id, count) => service.getMsgsIncludeSelf(endpoint, id, count, true)});
  check();
  return {...result, checkpoint: privateCheckpointOf(account, peer, upper)};
}
module.exports = {recoverPrivateWindow};
