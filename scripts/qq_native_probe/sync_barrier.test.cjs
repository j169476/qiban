'use strict';
// SPDX-License-Identifier: MIT
const test = require('node:test');
const assert = require('node:assert/strict');
const { MessageSyncBarrier } = require('./sync_barrier.cjs');
test('session and first-view completion do not imply full message sync', async () => {
  const gate = new MessageSyncBarrier(); let done = false;
  const waiting = gate.wait({ timeoutMs: 1000 }).then(x => { done = true; return x; });
  gate.observe('onSessionInitComplete'); gate.observe('onNtMsgSyncStart'); gate.observe('onNtFirstViewMsgSyncEnd');
  await Promise.resolve(); assert.equal(done, false);
  gate.observe('onNtMsgSyncEnd'); assert.equal(await waiting, 1); assert.equal(gate.waiters.size, 0);
});
test('completion received before wait is retained', async () => {
  const gate = new MessageSyncBarrier(); gate.observe('onNtMsgSyncStart'); gate.observe('onNtMsgSyncEnd');
  assert.equal(await gate.wait(), 1);
});
test('end without an observed start is not readiness', async () => {
  const gate = new MessageSyncBarrier(); gate.observe('onNtMsgSyncEnd');
  await assert.rejects(gate.wait({ timeoutMs: 5 }), /timeout/); assert.equal(gate.waiters.size, 0);
});
test('timeout is an error, never an empty history result', async () => {
  const gate = new MessageSyncBarrier(); gate.observe('onNtMsgSyncStart');
  await assert.rejects(gate.wait({ timeoutMs: 5 }), /timeout/); assert.equal(gate.phase, 'syncing');
});
test('cancel removes the pending waiter', async () => {
  const gate = new MessageSyncBarrier(); const controller = new AbortController();
  const pending = gate.wait({ signal: controller.signal }); controller.abort(Error('owner stopped'));
  await assert.rejects(pending, /owner stopped/); assert.equal(gate.waiters.size, 0);
});
test('already cancelled signal cannot pass even a ready gate', async () => {
  const gate = new MessageSyncBarrier(); gate.observe('onNtMsgSyncStart'); gate.observe('onNtMsgSyncEnd');
  await assert.rejects(gate.wait({ signal: AbortSignal.abort(Error('owner stopped')) }), /owner stopped/);
});
test('resync invalidates in-flight query results', async () => {
  const gate = new MessageSyncBarrier(); gate.observe('onNtMsgSyncStart'); gate.observe('onNtMsgSyncEnd');
  const generation = await gate.wait(); gate.observe('onNtMsgSyncStart');
  assert.throws(() => gate.assertCurrent(generation), /changed/);
  gate.observe('onNtMsgSyncEnd'); const next = await gate.wait(); assert.notEqual(next, generation); gate.assertCurrent(next);
});
test('disconnect invalidation requires a new complete cycle', async () => {
  const gate = new MessageSyncBarrier(); gate.observe('onNtMsgSyncStart'); gate.observe('onNtMsgSyncEnd');
  const old = await gate.wait(); gate.invalidate(); assert.throws(() => gate.assertCurrent(old), /changed/);
  gate.observe('onNtMsgSyncEnd'); assert.equal(gate.phase, 'waiting');
  gate.observe('onNtMsgSyncStart'); gate.observe('onNtMsgSyncEnd'); gate.assertCurrent(await gate.wait());
});

test('one cancelled consumer does not cancel other history consumers', async () => {
  const gate = new MessageSyncBarrier(), controller = new AbortController();
  const cancelled = assert.rejects(gate.wait({ signal: controller.signal }), /owner stopped/);
  const remaining = Array.from({length: 25}, () => gate.wait({timeoutMs: 1000}));
  controller.abort(Error('owner stopped'));
  gate.observe('onNtMsgSyncStart'); gate.observe('onNtMsgSyncEnd');
  await cancelled;
  assert.deepEqual(await Promise.all(remaining), Array(25).fill(1));
  assert.equal(gate.waiters.size, 0);
});

test('100 reconnect cycles reject stale results and release all waiters', async () => {
  const gate = new MessageSyncBarrier(); let previous;
  for (let cycle = 0; cycle < 100; cycle++) {
    gate.invalidate();
    if (previous !== undefined) assert.throws(() => gate.assertCurrent(previous), /changed/);
    const pending = gate.wait({timeoutMs: 1000});
    gate.observe('onNtMsgSyncEnd'); assert.equal(gate.phase, 'waiting');
    gate.observe('onNtMsgSyncStart'); gate.observe('onNtFirstViewMsgSyncEnd');
    assert.equal(gate.phase, 'syncing'); gate.observe('onNtMsgSyncEnd');
    previous = await pending; gate.assertCurrent(previous);
    assert.equal(gate.waiters.size, 0);
  }
});
