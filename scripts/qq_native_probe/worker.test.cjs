'use strict';
// SPDX-License-Identifier: MIT
const test = require('node:test'), assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const directory = path.resolve(__dirname, '../../src/companion_qq/native');
const nonce = 'a'.repeat(64), windowHash = require('node:crypto').createHash('sha256')
  .update(JSON.stringify(['10001', 'private', '10002', {seq: 'lower'}, {seq: 'upper'}])).digest('hex');
function run(commands, count = 101, options = {}) {
  // Stub the kernel session in a separate process; execute the actual stdio worker.
  const program = `
    const file = ${JSON.stringify(path.join(directory, 'session.cjs'))};
    class NativeSession {
      constructor() {
        this.catalog={uin:'10001',uid:'u_self',friends:{'10002':'u_peer'},groups:[]};
        this.gate={phase:'ready',generation:1,assertCurrent: n => {
          if(n!==1 || ${Boolean(options.stale)}) throw Error('Message sync changed during history query');
        }};
        this.media={download:async (row,position)=>({selected:row.msgSeq,position})};
      }
      guard() {}
      async open() {return this.catalog;}
      async groupHead(target) {if(target!=='10003') throw Error('qq_target_unverified'); return {seq:'group-head'};}
      async privateHead(target) {if(target!=='10002') throw Error('qq_target_unverified'); return {seq:'private-head'};}
      async groupWindow(group) {throw Error(group==='unknown'?'PRIVATE RAW VALUE':'No post-sync group head before deadline');}
      async privateWindow(target, lower, upper, size) {
        if (target!=='10002'||lower.seq!=='lower'||upper.seq!=='upper'||size!==2) throw Error('bad_arguments');
        return {checkpoint:upper,messages:Array.from({length:${count}},(_,i)=>({msgSeq:String(i+1),
          msgId:String(1000+i),msgTime:'1700000000',elements:[{elementId:'1'},{elementId:'2'},{elementId:'3'}],
          senderUin:${JSON.stringify(options.self ? '10001' : '10002')},
          senderUid:${JSON.stringify(options.self ? 'u_self' : 'u_peer')},
          peerUid:${JSON.stringify(options.wrongPeer ? 'u_other' : 'u_peer')}})),
          history_status:{status:'incomplete',reasons:['history_query_failed']}};
      }
      close() {}
    }
    require.cache[file]={id:file,filename:file,loaded:true,exports:{NativeSession}};
    require(${JSON.stringify(path.join(directory, 'worker.cjs'))});
  `;
  const input = [
    {command: 'open', args: {nonce, config: {}}},
    {command: 'private_open', args: {target: '10002', lower: {seq: 'lower'}, upper: {seq: 'upper'}, page_size: 2}},
    ...commands, {command: 'close', args: {}}
  ].map((request, index) => JSON.stringify({id: index + 1, ...request})).join('\n') + '\n';
  const child = spawnSync(process.execPath, ['-e', program], {input, encoding: 'utf8', timeout: 5000, windowsHide: true});
  assert.equal(child.status, 0, child.stderr);
  return child.stdout.split('\n').filter(line => line.startsWith('QIBAN_NATIVE:'))
    .map(line => JSON.parse(line.slice(('QIBAN_NATIVE:' + nonce + ':').length)));
}
test('head IPC routes explicit conversation targets without opening a new frozen range', () => {
  const replies = run([{command:'group_head',args:{target:'10003'}},
    {command:'private_head',args:{target:'10002'}},
    {command:'private_page',args:{window_id:windowHash,page:0}}]);
  assert.deepEqual(replies[2].result,{seq:'group-head'});
  assert.deepEqual(replies[3].result,{seq:'private-head'});
  assert.equal(replies[4].result.window_id,windowHash);
});

test('private IPC pages keep fixed bounds and incomplete status across the 100-row boundary', () => {
  const replies = run([0, 1].map(page => ({command: 'private_page', args: {window_id: windowHash, page}})));
  assert.equal(replies.length, 5);
  assert.equal(replies[1].result.count, 101);
  assert.equal(replies[1].result.window_id, windowHash);
  const first = replies[2].result, last = replies[3].result;
  assert.equal(first.kind, 'private'); assert.equal(first.target, '10002');
  assert.deepEqual(first.lower, {seq: 'lower'}); assert.deepEqual(first.upper, {seq: 'upper'});
  assert.equal(first.messages.length, 100); assert.equal(first.complete, false);
  assert.equal(last.messages.length, 1); assert.equal(last.complete, true);
  assert.deepEqual(last.history_status, {status: 'incomplete', reasons: ['history_query_failed']});
});
test('IPC rejects wrong conversation kind, wrong window and pages beyond the frozen range', () => {
  const replies = run([
    {command: 'group_page', args: {window_id: windowHash, page: 0}},
    {command: 'private_page', args: {window_id: 'other', page: 0}},
    {command: 'private_page', args: {window_id: windowHash, page: 2}}
  ]);
  for (const reply of replies.slice(2, 5)) assert.deepEqual({ok: reply.ok, error: reply.error},
    {ok: false, error: 'qq_native_protocol_invalid'});
});
test('empty private range has exactly one terminal page', () => {
  const replies = run([0, 1].map(page => ({command: 'private_page', args: {window_id: windowHash, page}})), 0);
  assert.deepEqual(replies[2].result.messages, []); assert.equal(replies[2].result.complete, true);
  assert.equal(replies[3].ok, false);
});

test('read diagnostics retain only fixed failure categories, never raw exception text', () => {
  for (const [group, reason] of [['10003', 'group_head_timeout'], ['unknown', 'unclassified']]) {
    const replies = run([{command: 'group_open', args: {group}}, {command: 'catalog_status', args: {}}]);
    assert.equal(replies[2].error, 'qq_read_group_open_' + reason);
    assert.deepEqual(replies[3].result.last_read_failure,
      {operation: 'group_open', reason, sync_phase: 'ready', sync_generation: 1});
    assert.equal(JSON.stringify(replies).includes('PRIVATE'), false);
  }
});

test('media command selects from the owned frozen window; caller cannot inject message or local path', () => {
  const replies = run([{command: 'download_attachment', args: {
    window_id: windowHash, index: 100, position: 2, message: {msgSeq: 'evil'}, filePath: 'C:/other',
    source:{msg_id:'1100',time:'1700000000',sender_uid:'u_peer',element_id:'3'}}}]);
  assert.deepEqual(replies[2].result, {selected: '101', position: 2});
});
test('media rejects another window, missing row, own echo, changed peer and stale synchronization', () => {
  const request = {command: 'download_attachment', args: {window_id: windowHash, index: 0, position: 0,
    source:{msg_id:'1000',time:'1700000000',sender_uid:'u_peer',element_id:'1'}}};
  for (const options of [{self: true}, {wrongPeer: true}, {stale: true}])
    assert.equal(run([request], 1, options)[2].ok, false);
  for (const args of [{window_id: 'other', index: 0}, {window_id: windowHash, index: 101},
    {window_id: windowHash, index: -1}])
    assert.equal(run([{...request, args}])[2].ok, false);
});
test('media rejects shifted or changed source locators before downloading', () => {
  const source={msg_id:'1000',time:'1700000000',sender_uid:'u_peer',element_id:'1'};
  for (const key of Object.keys(source)) {
    const reply=run([{command:'download_attachment',args:{window_id:windowHash,index:0,position:0,
      source:{...source,[key]:'changed'}}}],1)[2];
    assert.equal(reply.ok,false);
  }
});
