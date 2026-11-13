'use strict';
// SPDX-License-Identifier: MIT
const test = require('node:test'), assert = require('node:assert/strict');
const {checkpointOf, resolveCheckpoint} = require('./group_checkpoint.cjs');
const msg = patch => ({chatType: 2, peerUid: '20001', msgId: '9001', msgSeq: '41', senderUin: '10002', senderUid: 'u_other', msgTime: '1793339597', elements: [], ...patch});
const saved = () => checkpointOf('10001', '20001', msg());
const options = (messages, patch = {}) => ({account: '10001', group: '20001', query: async () => ({result: 0, msgList: messages}), assertCurrent() {}, ...patch});
test('cache-local ID changes do not change checkpoint identity', async () => {
  const checkpoint = saved();
  assert.equal('msgId' in checkpoint, false);
  assert.deepEqual(checkpointOf('10001', '20001', msg({msgId: '9010'})), checkpoint);
  assert.equal((await resolveCheckpoint(checkpoint, options([msg({msgId: '9010'})]))).msgId, '9010');
});
test('another account or group is rejected before any lookup', async () => {
  let calls = 0;
  for (const patch of [{account: '10003'}, {group: '20002'}]) await assert.rejects(resolveCheckpoint(saved(), options([], {...patch, query: async () => {calls++;}})), /account or group/);
  assert.equal(calls, 0);
});
test('matching sequence with a different author or time is not accepted', async () => {
  for (const patch of [{senderUin: '10003'}, {senderUid: 'u_wrong'}, {msgTime: '1793339598'}]) await assert.rejects(resolveCheckpoint(saved(), options([msg(patch)])), /author or time/);
});
test('missing and ambiguous checkpoints are explicit errors', async () => {
  for (const messages of [[], [msg(), msg({msgId: '9002'})], [msg({peerUid: '20002'})]]) await assert.rejects(resolveCheckpoint(saved(), options(messages)), /not uniquely resolved/);
});
test('a system-tip row sharing the checkpoint sequence cannot make it ambiguous', async () => {
  const tip = msg({msgId: '9900', msgType: 5, senderUin: '0', senderUid: '', elements: [{elementType: 8}]});
  assert.equal((await resolveCheckpoint(saved(), options([msg(), tip]))).msgId, '9001');
  await assert.rejects(resolveCheckpoint(saved(), options([tip])), /not uniquely resolved/);
});
test('lookup failure and generation changes cannot publish an endpoint', async () => {
  await assert.rejects(resolveCheckpoint(saved(), options([], {query: async () => ({result: -2, msgList: []})})), /lookup failed/);
  let active = true;
  await assert.rejects(resolveCheckpoint(saved(), options([], {query: async () => {active = false; return {result: 0, msgList: [msg()]};}, assertCurrent() {if (!active) throw Error('cancelled');}})), /cancelled/);
});
test('explicit partial-history policy permits a missing lower locator but not ambiguity', async () => {
  assert.equal(await resolveCheckpoint(saved(), options([], {allowMissing: true})), null);
  await assert.rejects(resolveCheckpoint(saved(), options([msg(), msg({msgId: '9002'})], {allowMissing: true})), /not uniquely resolved/);
});
