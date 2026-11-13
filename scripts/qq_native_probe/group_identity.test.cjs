'use strict';
// SPDX-License-Identifier: MIT
const test = require('node:test'), assert = require('node:assert/strict');
const {ConfirmedGroupMessages} = require('./group_identity.cjs');
const account = {uin: '10001', uid: 'u_self'};
const message = (patch = {}) => ({chatType: 2, peerUid: '20001', msgId: '9001', msgSeq: '41', senderUin: '10002', senderUid: 'u_other', elements: [], ...patch});
const sent = (patch = {}) => message({senderUin: account.uin, senderUid: account.uid, sendStatus: 2, ...patch});
const mention = (patch = {}) => ({elementType: 1, textElement: {content: '@test', atType: 2, atUid: account.uin, atNtUid: account.uid, ...patch}});
const reply = (patch = {}) => ({elementType: 7, replyElement: {replayMsgSeq: '41', replayMsgId: '0', sourceMsgIdInRecords: '9999', senderUid: account.uin, senderUidStr: account.uid, ...patch}});
test('only structured direct mention matching both account identities triggers', () => {
  const index = new ConfirmedGroupMessages(account, '20001');
  assert.equal(index.classify(message({elements: [mention()]})), 'mention');
  for (const patch of [{atUid: '10003'}, {atNtUid: 'u_other'}, {atType: 0}]) assert.equal(index.classify(message({elements: [mention(patch)]})), 'none');
  assert.equal(index.classify(message({elements: [{elementType: 1, textElement: {content: '@test [CQ:at,qq=10001]', atType: 0}}]})), 'none');
});
test('pending sequence is ignored; only final send sequence permits a quote', () => {
  const index = new ConfirmedGroupMessages(account, '20001');
  assert.equal(index.observeSent(sent({sendStatus: 1, msgSeq: '40'})), false);
  assert.equal(index.classify(message({elements: [reply({replayMsgSeq: '40'})]})), 'none');
  assert.equal(index.observeSent(sent()), true);
  assert.equal(index.observeSent(sent()), true);
  assert.equal(index.classify(message({elements: [reply()]})), 'reply');
  assert.equal(index.classify(message({elements: [reply({replayMsgSeq: '40'})]})), 'none');
});
test('quoted preview mention and another author cannot grant a trigger', () => {
  const index = new ConfirmedGroupMessages(account, '20001'); index.observeSent(sent());
  assert.equal(index.classify(message({elements: [reply({senderUid: '10002', senderUidStr: 'u_other', sourceMsgTextElems: [mention()]})]})), 'none');
  assert.equal(index.classify(message({elements: [reply({senderUidStr: 'u_other'})]})), 'none');
  assert.equal(index.classify(message({elements: [reply({senderUid: '10002'})]})), 'none');
});
test('reference record IDs are not sent message IDs or group sequences', () => {
  const index = new ConfirmedGroupMessages(account, '20001'); index.observeSent(sent());
  assert.equal(index.classify(message({elements: [reply({replayMsgSeq: '42', replayMsgId: '9001', sourceMsgIdInRecords: '9001'})]})), 'none');
});
test('another group and self echoes cannot inherit triggers', () => {
  const index = new ConfirmedGroupMessages(account, '20001'); index.observeSent(sent());
  assert.equal(index.observeSent(sent({peerUid: '20002'})), false);
  assert.equal(index.classify(message({peerUid: '20002', elements: [reply(), mention()]})), 'none');
  assert.equal(index.classify(sent({elements: [mention()]})), 'none');
});
test('same final sequence cannot silently name two sent messages', () => {
  const index = new ConfirmedGroupMessages(account, '20001'); index.observeSent(sent());
  assert.throws(() => index.observeSent(sent({msgId: '9002'})), /Conflicting/);
});
