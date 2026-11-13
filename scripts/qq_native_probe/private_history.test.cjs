'use strict';
// SPDX-License-Identifier: MIT
const test = require('node:test'), assert = require('node:assert/strict');
const {privateCheckpointOf, resolvePrivateCheckpoint} = require('./private_checkpoint.cjs');
const {readPrivateWindow} = require('./private_window.cjs');
const account = {uin: '10001', uid: 'u_self'}, peer = {uin: '10002', uid: 'u_peer'};
const msg = (seq, patch = {}) => ({chatType: 1, peerUid: peer.uid, senderUin: peer.uin, senderUid: peer.uid,
  msgId: String(9000 + seq), msgSeq: String(seq), clientSeq: String(5000 + seq), msgTime: '1700000000',
  msgRandom: String(100 + seq), elements: [{elementType: 1, textElement: {content: 'same text'}}], ...patch});
const ok = (...msgList) => ({result: 0, msgList});
const guard = () => {};
const checkpoint = privateCheckpointOf(account, peer, msg(1));
const lookup = (query, patch = {}) => ({account, peer, query, assertCurrent: guard, ...patch});
const window = (query, patch = {}) => ({peerUid: peer.uid, lower: msg(1), upper: msg(3), query, assertCurrent: guard, pageSize: 2, ...patch});

test('rebind a private checkpoint after local id and sequence change', async () => {
  let args;
  const rebound = msg(1, {msgId: '1234567890', msgSeq: '777'});
  const result = await resolvePrivateCheckpoint(checkpoint, lookup(async (...input) => {args = input; return ok(rebound);}));
  assert.deepEqual(args, ['5001', '1700000000']);
  assert.equal(result.msgSeq, '777');
  assert.equal(result.msgId, '1234567890');
  assert.deepEqual(privateCheckpointOf(account, peer, rebound), checkpoint);
});
test('account or peer switch rejects before querying', async () => {
  let calls = 0;
  for (const patch of [{account: {uin: '99999', uid: 'u_new'}}, {peer: {uin: '88888', uid: 'u_new'}}]) {
    await assert.rejects(resolvePrivateCheckpoint(checkpoint, lookup(async () => {calls++; return ok(msg(1));}, patch)), /account or peer changed/);
  }
  assert.equal(calls, 0);
});
test('same text with different source identity cannot satisfy a checkpoint', async () => {
  for (const patch of [{msgRandom: '999'}, {clientSeq: '999'}, {msgTime: '1700000001'}, {senderUin: account.uin, senderUid: account.uid}]) {
    await assert.rejects(resolvePrivateCheckpoint(checkpoint, lookup(async () => ok(msg(1, patch)))), /source identity changed/);
  }
});
test('missing checkpoint is explicit, while ambiguity and failed lookup remain errors', async () => {
  assert.equal(await resolvePrivateCheckpoint(checkpoint, lookup(async () => ok(), {allowMissing: true})), null);
  await assert.rejects(resolvePrivateCheckpoint(checkpoint, lookup(async () => ok())), /not uniquely resolved/);
  await assert.rejects(resolvePrivateCheckpoint(checkpoint, lookup(async () => ok(msg(1), msg(1)), {allowMissing: true})), /not uniquely resolved/);
  await assert.rejects(resolvePrivateCheckpoint(checkpoint, lookup(async () => ({result: -2}), {allowMissing: true})), /lookup failed/);
});
test('reconnect during lookup rejects returned data', async () => {
  let active = true;
  await assert.rejects(resolvePrivateCheckpoint(checkpoint, lookup(async () => {active = false; return ok(msg(1));},
    {assertCurrent() {if (!active) throw Error('generation changed');}})), /generation changed/);
});
test('equal text remains two messages while overlapping pages deduplicate identities', async () => {
  let calls = 0;
  const result = await readPrivateWindow(window(async () => ++calls === 1 ? ok(msg(2), msg(3)) : ok(msg(1), msg(2))));
  assert.deepEqual(result.messages.map(m => m.msgId), ['9002', '9003']);
  assert.equal(result.pages, 2); assert.equal(result.reachedCheckpoint, true);
  assert.deepEqual(result.history_status, {status: 'complete', reasons: []});
});
test('missing cross-cache lower does not apply an old numeric sequence', async () => {
  let calls = 0;
  const result = await readPrivateWindow(window(async () => ++calls === 1 ? ok(msg(2), msg(3)) : ok(), {lower: null}));
  assert.deepEqual(result.messages.map(m => m.msgSeq), ['2', '3']);
  assert.equal(result.reachedCheckpoint, false);
  assert.deepEqual(result.history_status, {status: 'incomplete', reasons: ['checkpoint_unavailable', 'history_unavailable']});
});
test('read failure after a page keeps readable private messages and an accurate reason', async () => {
  let calls = 0;
  const result = await readPrivateWindow(window(async () => ++calls === 1 ? ok(msg(2), msg(3)) : {result: -2}));
  assert.equal(result.messages.length, 2);
  assert.deepEqual(result.history_status, {status: 'incomplete', reasons: ['history_query_failed']});
});
test('another conversation or changed overlapping source cannot be merged', async () => {
  await assert.rejects(readPrivateWindow(window(async () => ok(msg(2, {peerUid: 'u_other'}), msg(3)))), /changed conversation/);
  let calls = 0;
  await assert.rejects(readPrivateWindow(window(async () => ++calls === 1 ? ok(msg(2), msg(3)) : ok(msg(1), msg(2, {msgRandom: '999'})))), /source changed/);
});
