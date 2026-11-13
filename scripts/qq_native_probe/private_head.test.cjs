'use strict';
// SPDX-License-Identifier: MIT
const test = require('node:test'), assert = require('node:assert/strict');
const {capturePrivateHead} = require('../../src/companion_qq/native/private_head.cjs');
const {MessageSyncBarrier} = require('./sync_barrier.cjs');
const account = {uin: '10001', uid: 'u_self'}, peer = {uin: '10002', uid: 'u_peer'};
const msg = n => ({chatType: 1, peerUid: peer.uid, senderUin: peer.uin, senderUid: peer.uid,
  msgSeq: String(n), clientSeq: String(n), msgTime: '1700000000', msgRandom: String(n)});
function options(query) {
  const gate = new MessageSyncBarrier(); gate.observe('onNtMsgSyncStart'); gate.observe('onNtMsgSyncEnd');
  return {account, peer, gate, guard() {}, service: {getLatestDbMsgs: query}};
}
test('private local fence uses exact sequence order and only returns stable source identity', async () => {
  const head = await capturePrivateHead(options(async (endpoint, count) => {
    assert.deepEqual(endpoint, {chatType: 1, peerUid: peer.uid, guildId: ''}); assert.equal(count, 20);
    return {result: 0, msgList: [msg('9007199254740993'), msg('9007199254740992')]};
  }));
  assert.equal(head.clientSeq, '9007199254740993'); assert.equal(head.msgSeq, undefined);
});
test('private local fence refuses failed, empty, cross-conversation and conflicting snapshots', async () => {
  for (const response of [{result: -1, msgList: [msg(1)]}, {result: 0, msgList: []},
    {result: 0, msgList: [{...msg(1), peerUid: 'u_other'}]},
    {result: 0, msgList: [msg(1), {...msg(1), msgRandom: '99'}]}])
    await assert.rejects(capturePrivateHead(options(async () => response)));
});
test('private local fence cannot survive synchronization generation change', async () => {
  const value = options(async () => {value.gate.observe('onNtMsgSyncStart'); return {result: 0, msgList: [msg(1)]};});
  await assert.rejects(capturePrivateHead(value), /generation|sync/i);
});
