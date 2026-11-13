'use strict';
// SPDX-License-Identifier: MIT
const test = require('node:test'), assert = require('node:assert/strict');
const {readGroupWindow} = require('./group_window.cjs');
const msg = (seq, patch = {}) => ({chatType: 2, peerUid: '20001', msgId: String(9000 + seq), msgSeq: String(seq), senderUin: '10002', senderUid: 'u_other', elements: [], ...patch});
const ok = (...messages) => ({result: 0, msgList: messages});
function options(query, patch = {}) {return {group: '20001', lower: msg(1), upper: msg(4), query, assertCurrent() {}, pageSize: 2, ...patch};}
test('inclusive overlapping pages are deduplicated and returned chronologically', async () => {
  const calls = [];
  const result = await readGroupWindow(options(async (id, size) => {calls.push([id, size]); const n = Number(id) - 9000; return ok(msg(n - 1), msg(n));}));
  assert.deepEqual(result.messages.map(m => m.msgSeq), ['2', '3', '4']);
  assert.equal(result.pages, 3); assert.equal(result.reachedCheckpoint, true);
  assert.deepEqual(calls, [['9004', 2], ['9003', 2], ['9002', 2]]);
});
test('messages newer than the frozen upper boundary are excluded', async () => {
  let n = 0;
  const result = await readGroupWindow(options(async () => ++n === 1 ? ok(msg(3), msg(4), msg(5)) : ok(msg(1), msg(2), msg(3)), {pageSize: 3}));
  assert.deepEqual(result.messages.map(m => m.msgSeq), ['2', '3', '4']);
});
test('a short page or empty page does not prove checkpoint completion', async () => {
  const short = await readGroupWindow(options(async () => ok(msg(4))));
  assert.deepEqual(short.history_status, {status: 'incomplete', reasons: ['history_read_stalled']});
  assert.equal(short.reachedCheckpoint, false); assert.equal(short.messages.length, 1);
  const empty = await readGroupWindow(options(async () => ok()));
  assert.deepEqual(empty.history_status, {status: 'incomplete', reasons: ['history_unavailable']});
  assert.equal(empty.reachedCheckpoint, false);
});
test('read failure and page limit retain available messages with an explicit incomplete reason', async () => {
  let n = 0;
  const failed = await readGroupWindow(options(async () => ++n === 1 ? ok(msg(3), msg(4)) : {result: -2, msgList: []}));
  assert.deepEqual(failed.messages.map(m => m.msgSeq), ['3', '4']);
  assert.deepEqual(failed.history_status, {status: 'incomplete', reasons: ['history_query_failed']});
  const limited = await readGroupWindow(options(async () => ok(msg(3), msg(4)), {maxPages: 1}));
  assert.deepEqual(limited.history_status, {status: 'incomplete', reasons: ['history_page_limit']});
});
test('another group is rejected', async () => {
  await assert.rejects(readGroupWindow(options(async () => ok(msg(3, {peerUid: 'other'}), msg(4)))), /changed conversation/);
});

test('failed query retains validated rows even when they include the checkpoint', async () => {
  let calls = 0;
  const result = await readGroupWindow(options(async () => {
    calls++; return {result: 2004000, msgList: [msg(1), msg(4)]};
  }));
  assert.equal(calls, 1);
  assert.deepEqual(result.messages.map(m => m.msgSeq), ['4']);
  assert.equal(result.reachedCheckpoint, false);
  assert.deepEqual(result.history_status, {status: 'incomplete', reasons: ['history_query_failed']});
});

test('failed query does not bypass source, size, overlap or response validation', async () => {
  const failed = (...msgList) => ({result: 2004000, msgList});
  await assert.rejects(readGroupWindow(options(async () => failed(msg(3, {peerUid: 'other'}), msg(4)))), /changed conversation/);
  await assert.rejects(readGroupWindow(options(async () => failed(msg(3, {msgSeq: 'bad'}), msg(4)))), /Invalid message identity/);
  await assert.rejects(readGroupWindow(options(async () => failed(msg(2), msg(3), msg(4)))), /exceeds requested bound/);
  await assert.rejects(readGroupWindow(options(async () => failed(msg(2), msg(3)))), /missing inclusive anchor/);
  await assert.rejects(readGroupWindow(options(async () => ({result: 2004000, msgList: {}}))), /Invalid history response/);
  let calls = 0;
  await assert.rejects(readGroupWindow(options(async () => ++calls === 1 ? ok(msg(3), msg(4)) : failed(msg(2), msg(3, {elements: ['edited']})))), /source changed/);
  const absent = await readGroupWindow(options(async () => ({result: 2004000})));
  assert.deepEqual(absent.history_status.reasons, ['history_query_failed']);
});
test('checkpoint mismatch or skipping past it is not silently accepted', async () => {
  await assert.rejects(readGroupWindow(options(async () => ok(msg(1, {msgId: '9999'}), msg(4)))), /identity changed/);
  const missing = await readGroupWindow(options(async () => ok(msg(1), msg(4)), {lower: msg(2)}));
  assert.deepEqual(missing.messages.map(m => m.msgSeq), ['4']);
  assert.deepEqual(missing.history_status, {status: 'incomplete', reasons: ['checkpoint_unavailable']});
});
test('source changes across overlapping pages invalidate the window', async () => {
  let n = 0;
  await assert.rejects(readGroupWindow(options(async () => ++n === 1 ? ok(msg(3), msg(4)) : ok(msg(2), msg(3, {elements: ['edited']})))), /source changed/);
});
test('resync or cancellation during a read rejects returned results', async () => {
  let active = true;
  await assert.rejects(readGroupWindow(options(async () => {active = false; return ok(msg(1), msg(4));}, {assertCurrent() {if (!active) throw Error('generation changed');}})), /generation changed/);
});
test('64-bit sequence comparison is exact', async () => {
  const lower = msg(1, {msgSeq: '9007199254740992'}), upper = msg(2, {msgSeq: '9007199254740993'});
  const result = await readGroupWindow(options(async () => ok(lower, upper), {lower, upper}));
  assert.deepEqual(result.messages.map(m => m.msgId), [upper.msgId]);
});
test('system tip sharing the lower sequence is traversed until the real checkpoint', async () => {
  const tip = msg(1, {msgId: '9900', msgType: 5, senderUin: '0', senderUid: '', elements: [{elementType: 8}]});
  const pages = new Map([['9003', ok(msg(2), msg(3))], ['9002', ok(tip, msg(2))], ['9900', ok(msg(1), tip)]]);
  const result = await readGroupWindow(options(async id => pages.get(id), {upper: msg(3)}));
  assert.equal(result.pages, 3);
  assert.deepEqual(result.messages.map(m => m.msgSeq), ['2', '3']);
});
test('ordinary messages sharing a sequence remain an error', async () => {
  await assert.rejects(readGroupWindow(options(async () => ok(msg(4, {msgId: '9900'}), msg(4)))), /source changed/);
});
test('same-sequence system-tip cursor cycle cannot loop forever', async () => {
  const tip = msg(4, {msgId: '9900', msgType: 5, senderUin: '0', senderUid: '', elements: [{elementType: 8}]});
  await assert.rejects(readGroupWindow(options(async () => ok(msg(4), tip))), /repeated a cursor/);
});
test('unavailable lower locator preserves its sequence boundary and incompleteness', async () => {
  const result = await readGroupWindow(options(async () => ok(msg(1), msg(4)), {lower: {msgId: null, msgSeq: '2'}}));
  assert.deepEqual(result.messages.map(m => m.msgSeq), ['4']);
  assert.equal(result.reachedCheckpoint, false);
  assert.deepEqual(result.history_status, {status: 'incomplete', reasons: ['checkpoint_unavailable']});
});
test('query cancellation is not converted to incomplete history', async () => {
  await assert.rejects(readGroupWindow(options(async () => {throw Error('account cancelled');})), /account cancelled/);
});
