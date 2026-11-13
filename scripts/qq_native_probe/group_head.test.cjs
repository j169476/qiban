'use strict';
// SPDX-License-Identifier: MIT
const test = require('node:test'), assert = require('node:assert/strict');
const {MessageSyncBarrier} = require('./sync_barrier.cjs');
const {PostSyncGroupHead, advanceGroupHead} = require('./group_head.cjs');
const contact = (seq = '41', patch = {}) => ({chatType: 2, peerUid: '20001', msgSeq: seq, senderUin: '10002', senderUid: 'u_other', msgTime: '1793339597', sendStatus: 2, ...patch});
function setup() {const gate = new MessageSyncBarrier(); return {gate, head: new PostSyncGroupHead('10001', '20001', gate)};}
function ready(gate) {gate.observe('onNtMsgSyncStart'); gate.observe('onNtMsgSyncEnd'); return gate.generation;}
const notify = (head, ...contacts) => head.observe('onRecentContactNotification', [contacts, false, 0]);
test('pre-sync cached contacts and global sync alone cannot produce a group head', async () => {
  const {gate, head} = setup(); notify(head, contact()); const generation = ready(gate);
  await assert.rejects(head.wait({generation, timeoutMs: 5}), /No post-sync group head/);
  assert.equal(head.waiters.size, 0);
});
test('target group post-sync notification completes wait and later arrivals do not move the fence', async () => {
  const {gate, head} = setup(); const generation = ready(gate), pending = head.wait({generation});
  notify(head, contact('40'), contact('42')); const checkpoint = await pending;
  assert.equal(checkpoint.seq, '42'); notify(head, contact('43'));
  assert.equal((await head.wait({generation})).seq, '42'); checkpoint.seq = '999';
  assert.equal((await head.wait({generation})).seq, '42');
});
test('list-changed callback uses changedList and ignores unrelated or pending contacts', async () => {
  const {gate, head} = setup(); const generation = ready(gate);
  notify(head, contact('42', {peerUid: '20002'}), contact('43', {sendStatus: 1}));
  assert.equal(head.head, null);
  head.observe('onRecentContactListChangedVer2', [[{changedList: [contact('41')]}], false]);
  assert.equal((await head.wait({generation})).seq, '41');
});
test('old generation head is not reused after reconnect', async () => {
  const {gate, head} = setup(); const old = ready(gate); notify(head, contact());
  const generation = ready(gate);
  await assert.rejects(head.wait({generation: old}), /changed/);
  const pending = head.wait({generation}); notify(head, contact('42'));
  assert.equal((await pending).seq, '42');
});
test('cancelled wait is removed and cached head cannot bypass cancellation', async () => {
  const {gate, head} = setup(); const generation = ready(gate), control = new AbortController();
  const pending = head.wait({generation, signal: control.signal}); control.abort(Error('owner stopped'));
  await assert.rejects(pending, /owner stopped/); assert.equal(head.waiters.size, 0);
  notify(head, contact()); await assert.rejects(head.wait({generation, signal: control.signal}), /owner stopped/);
});
test('reconnect while waiting cannot resolve an old generation with a new head', async () => {
  const {gate, head} = setup(); const generation = ready(gate), pending = head.wait({generation});
  ready(gate); notify(head, contact('42')); await assert.rejects(pending, /changed/);
});
test('newly received local messages advance a stale post-sync contact candidate', async () => {
  const {gate, head} = setup(); const generation = ready(gate); notify(head, contact('41'));
  const candidate = await head.wait({generation});
  const upper = advanceGroupHead(candidate, [contact('41'), contact('42'), contact('43')]);
  assert.equal(candidate.seq, '41'); assert.equal(upper.seq, '43');
  assert.equal(advanceGroupHead(upper, [contact('42')]).seq, '43');
});
test('snapshot tips cannot shadow the head and other conversations are rejected', async () => {
  const {gate, head} = setup(); const generation = ready(gate); notify(head, contact('41'));
  const candidate = await head.wait({generation});
  const tip = contact('41', {msgType: 5, senderUin: '0', senderUid: '', elements: [{elementType: 8}]});
  assert.equal(advanceGroupHead(candidate, [tip]).seq, '41');
  assert.throws(() => advanceGroupHead(candidate, [contact('42', {peerUid: '20002'})]), /changed conversation/);
  assert.throws(() => advanceGroupHead(candidate, [contact('41', {senderUid: 'u_wrong'})]), /identity conflict/);
});
