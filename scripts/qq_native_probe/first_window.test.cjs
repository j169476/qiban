const test=require('node:test'), assert=require('node:assert/strict');
const {readWindow}=require('../../src/companion_qq/native/message_window.cjs');
for(const kind of [1,2])test('first offline window filters pre-grant time, includes boundary, chat type '+kind,async()=>{
 const make=(i,t)=>({msgId:String(i),msgSeq:String(i),chatType:kind,peerUid:'peer',msgTime:String(t)});
 const list=[make(1,98),make(2,99),make(3,100),make(4,101)];
 const result=await readWindow({chatType:kind,peerUid:'peer',lower:null,upper:list[3],since:100,
    skipRecord:()=>false,assertCurrent(){},pageSize:3,query:async id=>({result:0,msgList:list.slice(Math.max(0,Number(id)-3),Number(id)).reverse()})});
 assert.deepEqual(result.messages.map(m=>m.msgId),['3','4']);assert.equal(result.history_status.status,'complete');
});
