'use strict';
// SPDX-License-Identifier: MIT
const test = require('node:test'), assert = require('node:assert/strict');
const {TextSender} = require('../../src/companion_qq/native/text_sender.cjs');
function fixture() {
  let next = 100, generation = 1;
  const sent = [];
  const value = new TextSender({account: {uin: '10001', uid: 'u_self'}, serverTime: () => '1700000000',
    guard: () => generation, timeoutMs: 30,
    resolvePeer: (kind, target) => {
      if (kind !== 'group' || target !== '10003') throw Error('qq_target_unverified');
      return {chatType: 2, peerUid: target, guildId: ''};
    }, service: {generateMsgUniqueId: () => String(++next), sendMsg: async (id, peer, elements) => {
      sent.push({id, peer, elements}); return {result: 0};
    }}});
  const row = (id, patch = {}) => ({msgId: id, msgSeq: String(Number(id) + 1000), chatType: 2, peerUid: '10003',
    senderUin: '10001', senderUid: 'u_self', sendStatus: 2,
    elements: [{elementType: 1, textElement: {content: 'same 中文🙂', atType: 0}}], ...patch});
  return {value, sent, row, resync: () => generation++};
}
test('same text gets distinct IDs; duplicate callbacks and used tickets cannot resubmit', async () => {
  const {value, sent, row} = fixture();
  const first = value.prepare('group', '10003', 'same 中文🙂');
  const sending = value.submit(first.token);
  value.observe('onMsgInfoListUpdate', [row(first.message_id), row(first.message_id)]);
  assert.deepEqual(await sending, {message_id: first.message_id, reference: 'native-seq:1101'});
  await assert.rejects(value.submit(first.token), /send_state/);
  const second = value.prepare('group', '10003', 'same 中文🙂');
  assert.notEqual(second.message_id, first.message_id);
  const later = value.submit(second.token);
  value.observe('onMsgInfoListUpdate', [row(first.message_id), row(second.message_id)]);
  assert.equal((await later).message_id, second.message_id);
  assert.equal(sent.length, 2);
});
test('prepare cancellation, wrong target and generation change never call sendMsg', async () => {
  const {value, sent, resync} = fixture();
  assert.throws(() => value.prepare('group', '99999', 'text'), /target_unverified/);
  const first = value.prepare('group', '10003', 'text'); value.cancel(first.token);
  await assert.rejects(value.submit(first.token), /send_state/);
  const second = value.prepare('group', '10003', 'text'); resync();
  await assert.rejects(value.submit(second.token), /send_state/);
  assert.equal(sent.length, 0);
});
test('wrong author, scope, ID and pending callbacks cannot confirm; timeout never retries', async () => {
  const {value, sent, row} = fixture();
  const prepared = value.prepare('group', '10003', 'same 中文🙂');
  const sending = value.submit(prepared.token);
  for (const patch of [{senderUid: 'u_other'}, {senderUin: '99999'}, {peerUid: 'other'}, {msgId: '1'}, {sendStatus: 1}])
    value.observe('onMsgInfoListUpdate', [row(prepared.message_id, patch)]);
  await assert.rejects(sending, /send_uncertain/);
  await assert.rejects(value.submit(prepared.token), /send_state/);
  assert.throws(() => value.prepare('group', '10003', 'text'), /send_state/);
  assert.equal(sent.length, 1);
});
test('native acceptance alone, conflicting text and shutdown never become a final receipt', async () => {
  for (const mode of ['timeout', 'conflict', 'close']) {
    const {value, row, sent} = fixture();
    const prepared = value.prepare('group', '10003', 'same 中文🙂');
    const sending = value.submit(prepared.token);
    if (mode === 'conflict') value.observe('onMsgInfoListUpdate', [row(prepared.message_id, {elements: []})]);
    if (mode === 'close') value.close();
    await assert.rejects(sending, /send_uncertain/);
    if (mode === 'close') assert.equal(sent.length, 0);
  }
});

test('a final callback cannot override a failed native submit result', async () => {
  const {value, row} = fixture();
  value.service.sendMsg = async () => ({result: -1});
  const prepared = value.prepare('group', '10003', 'same 中文🙂');
  const sending = value.submit(prepared.token);
  value.observe('onMsgInfoListUpdate', [row(prepared.message_id)]);
  await assert.rejects(sending, /send_uncertain/);
});


test('voice uses the same single-submit receipt and removes its prepared file after confirmation', async () => {
  const fs=require('node:fs'), os=require('node:os'), path=require('node:path'), crypto=require('node:crypto');
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'qiban-voice-test-'));
  const {value,sent,row}=fixture();value.mediaRoot=root;
  const audio=Buffer.concat([Buffer.from('\x02#!SILK_V3'),Buffer.alloc(40)]);
  try {
    const ticket=value.prepareVoice('group','10003',audio.toString('base64'),3000);
    assert.equal(fs.readdirSync(root).length,1);
    const sending=value.submit(ticket.token);
    value.observe('onMsgInfoListUpdate',[row(ticket.message_id,{elements:[{elementType:4,
      pttElement:{md5HexStr:crypto.createHash('md5').update(audio).digest('hex')}}]})]);
    await sending;
    assert.equal(sent.length,1);assert.equal(sent[0].elements[0].pttElement.duration,3);
    assert.equal(fs.readdirSync(root).length,0);
    await assert.rejects(value.submit(ticket.token),/send_state/);
  } finally {value.close();for(const file of fs.readdirSync(root))fs.unlinkSync(path.join(root,file));fs.rmdirSync(root);}
});

test('voice cancellation removes preparation; lost receipt cannot become a text retry', async () => {
  const fs=require('node:fs'), os=require('node:os'), path=require('node:path');
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'qiban-voice-test-'));
  const {value,sent}=fixture();value.mediaRoot=root;
  const audio=Buffer.concat([Buffer.from('\x02#!SILK_V3'),Buffer.alloc(40)]).toString('base64');
  try {
    const cancelled=value.prepareVoice('group','10003',audio,3000);value.cancel(cancelled.token);
    assert.equal(fs.readdirSync(root).length,0);assert.equal(sent.length,0);
    const ticket=value.prepareVoice('group','10003',audio,3000);
    await assert.rejects(value.submit(ticket.token),/send_uncertain/);
    assert.equal(sent.length,1);
    assert.throws(()=>value.prepare('group','10003','fallback'),/send_state/);
  } finally {value.close();for(const file of fs.readdirSync(root))fs.unlinkSync(path.join(root,file));fs.rmdirSync(root);}
});


test('image prepare preserves bytes, cancellation cleans up, and confirmed MD5 cannot resend', async () => {
  const fs=require('node:fs'), os=require('node:os'), path=require('node:path'), crypto=require('node:crypto');
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'qiban-image-test-'));
  const {value,sent,row}=fixture();value.mediaRoot=root;value.imageCacheRoot=root;
  value.service.getRichMediaFilePathForGuild=info=>path.join(root,info.md5HexStr+'.png');
  const bytes=Buffer.from('89504e470d0a1a0a010203','hex');
  try {
    assert.throws(()=>value.prepareImage('group','10003',bytes.toString('base64'),0,20,'PNG'));
    assert.throws(()=>value.prepareImage('group','10003',bytes.toString('base64'),20,20,'GIF'));
    const cancelled=value.prepareImage('group','10003',bytes.toString('base64'),20,20,'PNG');
    value.cancel(cancelled.token);assert.equal(fs.readdirSync(root).length,1);
    const ticket=value.prepareImage('group','10003',bytes.toString('base64'),20,20,'PNG');
    assert.deepEqual(fs.readFileSync(value.pending.image.file),bytes);
    assert.deepEqual(fs.readFileSync(value.pending.image.source),bytes);
    const sending=value.submit(ticket.token);
    value.observe('onMsgInfoListUpdate',[row(ticket.message_id,{elements:[{elementType:2,
      picElement:{md5HexStr:crypto.createHash('md5').update(bytes).digest('hex')}}]})]);
    await sending;assert.equal(sent.length,1);assert.equal(sent[0].elements[0].picElement.original,true);
    assert.equal(fs.readdirSync(root).length,1);await assert.rejects(value.submit(ticket.token),/send_state/);
  } finally {value.close();for(const file of fs.readdirSync(root))fs.unlinkSync(path.join(root,file));fs.rmdirSync(root);}
});

test('image conflicting receipt is uncertain and never retries', async () => {
  const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'qiban-image-test-'));
  const {value,sent,row}=fixture();value.mediaRoot=root;value.imageCacheRoot=root;
  value.service.getRichMediaFilePathForGuild=info=>path.join(root,info.md5HexStr+'.gif');
  try {
    const ticket=value.prepareImage('group','10003',Buffer.from('GIF89a1234').toString('base64'),20,20,'GIF');
    const sending=value.submit(ticket.token);
    value.observe('onMsgInfoListUpdate',[row(ticket.message_id,{elements:[{elementType:2,picElement:{md5HexStr:'0'.repeat(32)}}]})]);
    await assert.rejects(sending,/send_uncertain/);assert.equal(sent.length,1);
    assert.throws(()=>value.prepare('group','10003','retry'),/send_state/);
  } finally {value.close();for(const file of fs.readdirSync(root))fs.unlinkSync(path.join(root,file));fs.rmdirSync(root);}
});


test('native rejection is diagnosed immediately without awaiting an absent callback', async () => {
  const {value} = fixture();value.timeoutMs=10000;
  value.service.sendMsg=async()=>({result:120});
  const ticket=value.prepare('group','10003','text');
  await assert.rejects(value.submit(ticket.token),/send_uncertain/);
  assert.deepEqual(value.diagnostic,{phase:'native_rejected',native_result:120,event_status:null});
});


test('image -1 waits for the exact final receipt; acceptance or wrong digest alone never succeeds', async () => {
  const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
  for(const confirm of [true,false]) {
    const root=fs.mkdtempSync(path.join(os.tmpdir(),'qiban-image-async-'));
    const {value,sent,row}=fixture();value.mediaRoot=root;value.imageCacheRoot=root;
    value.service.getRichMediaFilePathForGuild=info=>path.join(root,info.md5HexStr+'.gif');
    const original=value.service.sendMsg;
    value.service.sendMsg=async(...args)=>{await original(...args);return {result:-1};};
    const bytes=Buffer.from('GIF89a1234');
    try {
      const ticket=value.prepareImage('group','10003',bytes.toString('base64'),20,20,'GIF');
      const sending=value.submit(ticket.token);
      if(confirm) {
        value.observe('onMsgInfoListUpdate',[row(ticket.message_id,{elements:[{elementType:2,
          picElement:{md5HexStr:crypto.createHash('md5').update(bytes).digest('hex')}}]})]);
        assert.equal((await sending).message_id,ticket.message_id);
      } else await assert.rejects(sending,/send_uncertain/);
      assert.equal(sent.length,1);
    } finally {value.close();for(const file of fs.readdirSync(root))fs.unlinkSync(path.join(root,file));fs.rmdirSync(root);}
  }
});


test('files preserve original bytes and Chinese name; cancel, receipt loss and mismatch never retry', async () => {
 const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
 for(const mode of ['success','cancel','timeout','wrong-name','wrong-size','wrong-md5']){
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'qiban-file-test-'));
  const {value,sent,row}=fixture();value.mediaRoot=root;value.fileCacheRoot=path.join(root,'cache');
  const bytes=Buffer.from('原始文件中文🙂'),name='中文说明.txt',md5=crypto.createHash('md5').update(bytes).digest('hex');
  value.service.getRichMediaFilePathForGuild=args=>{assert.equal(args.elementType,3);assert.equal(args.fileName,name);return path.join(value.fileCacheRoot,md5+'.txt');};
  try{
   const ticket=value.prepareFile('group','10003',bytes.toString('base64'),name);
   assert.deepEqual(fs.readFileSync(value.pending.file.source),bytes);
   if(mode==='cancel'){value.cancel(ticket.token);assert.equal(sent.length,0);continue;}
   const sending=value.submit(ticket.token);
   if(mode!=='timeout')value.observe('onMsgInfoListUpdate',[row(ticket.message_id,{elements:[{elementType:3,fileElement:{
    fileName:mode==='wrong-name'?'other.txt':name,fileSize:mode==='wrong-size'?'1':String(bytes.length),fileMd5:mode==='wrong-md5'?'bad':md5}}]})]);
   if(mode==='success'){
    assert.equal((await sending).message_id,ticket.message_id);
    assert.equal(sent[0].elements[0].fileElement.fileName,name);
    assert.equal(fs.readdirSync(root).filter(n=>n.endsWith('.bin')).length,0);
   }else await assert.rejects(sending,/send_uncertain/);
   await assert.rejects(value.submit(ticket.token),/send_state/);assert.equal(sent.length,1);
  }finally{value.close();fs.rmSync(root,{recursive:true,force:true});}
 }
});
test('file names cannot supply paths or Windows device names',()=>{
 const {value,sent}=fixture();
 for(const name of ['../file','C:\\file','NUL.txt','bad.','bad\n.txt'])
  assert.throws(()=>value.prepareFile('group','10003',Buffer.from('test').toString('base64'),name),/protocol_invalid/);
 assert.equal(sent.length,0);assert.equal(value.pending,null);
});


test('a matching final receipt cannot leave an unresolved native return waiting forever', async()=>{
 const {value,row,sent}=fixture();
 value.service.sendMsg=(...args)=>{sent.push(args);return new Promise(()=>{});};
 const ticket=value.prepare('group','10003','same 中文🙂');
 const work=value.submit(ticket.token);
 value.observe('onMsgInfoListUpdate',[row(ticket.message_id)]);
 await assert.rejects(work,/send_uncertain/);
 assert.deepEqual(value.diagnostic,{phase:'native_return_timeout',native_result:null,event_status:2});
 assert.equal(sent.length,1);assert.equal(value.failed,true);
 await assert.rejects(value.submit(ticket.token),/send_state/);
 value.close();
});
test('pending native return without a final receipt also meets the whole-operation deadline', async()=>{
 const {value}=fixture();value.service.sendMsg=()=>new Promise(()=>{});
 const ticket=value.prepare('group','10003','same 中文🙂');
 await assert.rejects(value.submit(ticket.token),/send_uncertain/);
 assert.equal(value.diagnostic.phase,'receipt_timeout');value.close();
});


test('file final receipt completes with pending or media acceptance return; late return cannot affect next send', async()=>{
 const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
 for(const nativeMode of ['pending','media-accepted','accepted-no-receipt','pending-no-receipt','rejected']){
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'qiban-file-return-'));
  const {value,row}=fixture();value.mediaRoot=root;value.fileCacheRoot=path.join(root,'cache');
  const bytes=Buffer.from('file-original'),name='说明.txt',md5=crypto.createHash('md5').update(bytes).digest('hex');
  let resolveNative;
  value.service.getRichMediaFilePathForGuild=()=>path.join(value.fileCacheRoot,md5+'.txt');
  value.service.sendMsg=()=>nativeMode.startsWith('pending')?new Promise(resolve=>{resolveNative=resolve;}):Promise.resolve({result:nativeMode==='rejected'?120:nativeMode==='media-accepted'?-1:0});
  try{
   const ticket=value.prepareFile('group','10003',bytes.toString('base64'),name);
   const work=value.submit(ticket.token);
   if(nativeMode==='pending'||nativeMode==='media-accepted'){
    value.observe('onMsgInfoListUpdate',[row(ticket.message_id,{elements:[{elementType:3,fileElement:{fileName:name,fileSize:String(bytes.length),fileMd5:md5}}]})]);
    assert.equal((await work).message_id,ticket.message_id);
    assert.equal(value.diagnostic.phase,'confirmed');
    if(nativeMode==='pending'){
     const previous=value.diagnostic;value.service.sendMsg=async()=>({result:0});
     const next=value.prepare('group','10003','same 中文🙂'),sending=value.submit(next.token);
     resolveNative({result:120});
     value.observe('onMsgInfoListUpdate',[row(next.message_id)]);await sending;
     assert.equal(previous.phase,'confirmed');assert.equal(previous.native_result,120);
     assert.deepEqual(value.diagnostic,{phase:'confirmed',native_result:0,event_status:2});
    }
   }else{
    await assert.rejects(work,/send_uncertain/);
    assert.equal(value.diagnostic.phase,nativeMode==='rejected'?'native_rejected':'receipt_timeout');
   }
  }finally{value.close();fs.rmSync(root,{recursive:true,force:true});}
 }
});
