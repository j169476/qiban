'use strict';
// SPDX-License-Identifier: MIT
const test = require('node:test'), assert = require('node:assert/strict');
const {recoverPrivateWindow} = require('../../src/companion_qq/native/private_recovery.cjs');
const {privateCheckpointOf} = require('./private_checkpoint.cjs');
const {MessageSyncBarrier} = require('./sync_barrier.cjs');
const account = {uin: '10001', uid: 'u_self'}, peer = {uin: '10002', uid: 'u_peer'};
const msg = n => ({chatType: 1, peerUid: peer.uid, senderUin: peer.uin, senderUid: peer.uid,
  msgId: String(9000 + n), msgSeq: String(n), clientSeq: String(5000 + n),
  msgTime: '1700000000', msgRandom: String(n), elements: []});
const ok = (...msgList) => ({result: 0, msgList});
function options() {
  const gate = new MessageSyncBarrier(); gate.observe('onNtMsgSyncStart'); gate.observe('onNtMsgSyncEnd');
  return {account, peer, gate, guard() {}, pageSize: 2,
    lowerCheckpoint: privateCheckpointOf(account, peer, msg(1)),
    upperCheckpoint: privateCheckpointOf(account, peer, msg(3)),
    service: {
      async getMsgByClientSeqAndTime(endpoint, seq, time) {
        assert.deepEqual(endpoint, {chatType: 1, peerUid: peer.uid, guildId: ''});
        assert.equal(time, '1700000000'); return ok(msg(Number(seq) - 5000));
      },
      async getMsgsIncludeSelf(endpoint, id, count, inclusive) {
        assert.equal(count, 2); assert.equal(inclusive, true);
        const n = Number(id) - 9000; return ok(msg(n - 1), msg(n));
      }
    }};
}
test('private recovery rebinds fixed source endpoints and deduplicates overlapping pages', async () => {
  const value = options(), result = await recoverPrivateWindow(value);
  assert.deepEqual(result.messages.map(m => m.msgSeq), ['2', '3']);
  assert.deepEqual(result.checkpoint, value.upperCheckpoint);
  assert.deepEqual(result.history_status, {status: 'complete', reasons: []});
});
test('missing private lower retains available rows and does not reuse old cache sequence', async () => {
  const value = options(), original = value.service.getMsgByClientSeqAndTime;
  value.service.getMsgByClientSeqAndTime = (p, seq, t) => seq === '5001' ? ok() : original(p, seq, t);
  let calls = 0;
  value.service.getMsgsIncludeSelf = async () => ++calls === 1 ? ok(msg(2), msg(3)) : ok();
  const result = await recoverPrivateWindow(value);
  assert.equal(result.messages.length, 2); assert.equal(result.reachedCheckpoint, false);
  assert.deepEqual(result.history_status.reasons, ['checkpoint_unavailable', 'history_unavailable']);
});
test('failed query retains private rows with explicit incompleteness', async () => {
  const value = options();
  value.service.getMsgsIncludeSelf = async () => ({result: 2004000, msgList: [msg(2), msg(3)]});
  const result = await recoverPrivateWindow(value);
  assert.equal(result.messages.length, 2); assert.equal(result.reachedCheckpoint, false);
  assert.deepEqual(result.history_status.reasons, ['history_query_failed']);
});
test('wrong upper owner, missing upper and resync are rejected before delivery', async () => {
  const wrong = options(); wrong.upperCheckpoint.accountUin = '99999';
  wrong.service.getMsgByClientSeqAndTime = () => {throw Error('must not query');};
  await assert.rejects(recoverPrivateWindow(wrong), /account or peer changed/);
  const missing = options(); missing.service.getMsgByClientSeqAndTime = async () => ok();
  await assert.rejects(recoverPrivateWindow(missing), /not uniquely resolved/);
  const resync = options();
  resync.service.getMsgsIncludeSelf = async () => {resync.gate.observe('onNtMsgSyncStart'); return ok(msg(2), msg(3));};
  await assert.rejects(recoverPrivateWindow(resync), /generation|sync/i);
});
