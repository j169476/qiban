'use strict';
// SPDX-License-Identifier: MIT
const test = require('node:test'), assert = require('node:assert/strict');
const {recoverGroupWindow} = require('./group_recovery.cjs');
const {checkpointOf} = require('./group_checkpoint.cjs');
const {MessageSyncBarrier} = require('./sync_barrier.cjs');
const {PostSyncGroupHead} = require('./group_head.cjs');
const msg = seq => ({chatType: 2, peerUid: '20001', msgId: String(9000 + seq), msgSeq: String(seq), msgTime: '1793339597',
  senderUin: '10002', senderUid: 'u_other', sendStatus: 2, elements: []});
test('resume keeps the persisted upper bound without consulting a later head', async () => {
  const input = options();
  input.upperCheckpoint = checkpointOf('10001', '20001', msg(2));
  input.headGate.wait = async () => {throw Error('must not replace saved upper');};
  input.contactQuery = async () => {throw Error('must not query a new upper');};
  input.service.getLatestDbMsgs = async () => {throw Error('must not take a fresh head');};
  const result = await recoverGroupWindow(input);
  assert.equal(result.checkpoint.seq, '2');
  assert.deepEqual(result.messages.map(message => message.msgSeq), ['2']);
});
function options() {
  const gate = new MessageSyncBarrier(); gate.observe('onNtMsgSyncStart'); gate.observe('onNtMsgSyncEnd');
  const headGate = new PostSyncGroupHead('10001', '20001', gate);
  headGate.observe('onRecentContactNotification', [[msg(1)], false, 0]);
  return {account: '10001', group: '20001', lowerCheckpoint: checkpointOf('10001', '20001', msg(1)), gate, headGate,
    guard() {}, pageSize: 2, service: {
      async getLatestDbMsgs() {return {result: 0, msgList: [msg(1), msg(2), msg(3)]};},
      async getSingleMsg(peer, seq) {return {result: 0, msgList: [msg(Number(seq))]};},
      async getMsgsIncludeSelf(peer, id) {const n = Number(id) - 9000; return {result: 0, msgList: [msg(n - 1), msg(n)]};},
    }};
}
test('shared recovery advances stale head and reports complete only after known checkpoint', async () => {
  const result = await recoverGroupWindow(options());
  assert.deepEqual(result.messages.map(m => m.msgSeq), ['2', '3']);
  assert.equal(result.checkpoint.seq, '3');
  assert.deepEqual(result.history_status, {status: 'complete', reasons: []});
});
test('missing checkpoint continues available history with required reason', async () => {
  const value = options(), old = value.service.getSingleMsg;
  value.service.getSingleMsg = async (peer, seq) => seq === '1' ? {result: 0, msgList: []} : old(peer, seq);
  const result = await recoverGroupWindow(value);
  assert.deepEqual(result.messages.map(m => m.msgSeq), ['2', '3']);
  assert.equal(result.reachedCheckpoint, false);
  assert.deepEqual(result.history_status, {status: 'incomplete', reasons: ['checkpoint_unavailable']});
});
test('partial later-page failure keeps messages and an honest failure reason', async () => {
  const value = options(), original = value.service.getMsgsIncludeSelf; let calls = 0;
  value.service.getMsgsIncludeSelf = (...args) => ++calls === 1 ? original(...args) : Promise.resolve({result: -2, msgList: []});
  const result = await recoverGroupWindow(value);
  assert.equal(result.messages.length, 2);
  assert.deepEqual(result.history_status, {status: 'incomplete', reasons: ['history_query_failed']});
});
test('account cancellation is never treated as missing history', async () => {
  const value = options(), controller = new AbortController(); value.signal = controller.signal;
  value.service.getLatestDbMsgs = async () => {controller.abort(Error('account changed')); return {result: 0, msgList: [msg(3)]};};
  await assert.rejects(recoverGroupWindow(value), /account changed/);
});

test('post-sync explicit contact lookup works without a recent-contact notification', async () => {
  const value = options(); let calls = 0;
  value.headGate.wait = async () => {throw Error('notification must not be required');};
  value.contactQuery = async peers => {
    calls++;
    assert.equal(value.gate.phase, 'ready');
    assert.deepEqual(peers, [{chatType: 2, peerUid: '20001', guildId: ''}]);
    return {result: 0, foundContacts: [msg(1)], unFound: []};
  };
  const result = await recoverGroupWindow(value);
  assert.equal(calls, 1);
  assert.equal(result.checkpoint.seq, '3');
  assert.deepEqual(result.messages.map(m => m.msgSeq), ['2', '3']);
});

test('unavailable, ambiguous, wrong-group and pending contact snapshots cannot establish an upper', async () => {
  for (const patch of [{result: -1}, {foundContacts: []}, {foundContacts: [msg(1), msg(2)]},
    {unFound: ['missing']}, {foundContacts: [{...msg(1), peerUid: 'other'}]},
    {foundContacts: [{...msg(1), sendStatus: 1}]}]) {
    const value = options();
    value.contactQuery = async () => ({result: 0, foundContacts: [msg(1)], unFound: [], ...patch});
    value.service.getLatestDbMsgs = async () => {throw Error('invalid contact must stop before local query');};
    await assert.rejects(recoverGroupWindow(value), /snapshot unavailable|checkpoint identity/);
  }
});

test('a resync during explicit contact lookup rejects the returned contact', async () => {
  const value = options();
  value.contactQuery = async () => {
    value.gate.observe('onNtMsgSyncStart');
    return {result: 0, foundContacts: [msg(3)], unFound: []};
  };
  await assert.rejects(recoverGroupWindow(value), /sync changed/);
});
