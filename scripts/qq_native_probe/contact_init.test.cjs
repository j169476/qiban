'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const {NativeSession} = require('../../src/companion_qq/native/session.cjs');
function setup() {
  const owner = new NativeSession({account:{uin:'10001',uid:'u_self'}});
  owner.ready = true;
  owner.gate.observe('onNtMsgSyncStart');owner.gate.observe('onNtMsgSyncEnd');
  const calls = [];
  const recent = {
    async getRecentContactList() {calls.push('initialize');return {result:0};},
    async getContacts(peers) {calls.push(peers);return {result:0,foundContacts:[],unFound:peers};}
  };
  owner.session={getRecentContactService:()=>recent};
  return {owner,recent,calls};
}
test('fresh contact index is initialized before querying, once per sync cycle', async () => {
  const {owner,calls}=setup();const peers=[{chatType:2,peerUid:'10003'}];
  await owner.queryContacts(peers);await owner.queryContacts(peers);
  assert.deepEqual(calls,['initialize',peers,peers]);
  owner.gate.observe('onNtMsgSyncStart');owner.gate.observe('onNtMsgSyncEnd');
  await owner.queryContacts(peers);
  assert.deepEqual(calls,['initialize',peers,peers,'initialize',peers]);
});
test('initialization failure is explicit and does not query or mark ready', async () => {
  const {owner,recent,calls}=setup();
  recent.getRecentContactList=async()=>({result:1});
  await assert.rejects(owner.queryContacts([]),/initialization failed/);
  assert.deepEqual(calls,[]);assert.equal(owner.contactsGeneration,undefined);
});
test('sync change during initialization invalidates its result', async () => {
  const {owner,recent,calls}=setup();
  recent.getRecentContactList=async()=>{owner.gate.observe('onNtMsgSyncStart');return {result:0};};
  await assert.rejects(owner.queryContacts([]),/sync changed/);
  assert.deepEqual(calls,[]);assert.equal(owner.contactsGeneration,undefined);
});
