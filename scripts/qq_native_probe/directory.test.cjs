'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {friends,groups,readDirectory}=require('../../src/companion_qq/native/directory.cjs');
test('directory exposes only identifiers and names, with remarks preferred',()=>{
 assert.deepEqual(friends([{buddyList:[{uin:'10001',uid:'u_private',nick:'昵称',remark:'备注🙂',phone:'secret'}]}]),[{id:'10001',label:'备注🙂',nickname:'昵称'}]);
 assert.deepEqual(friends([{buddyList:[{uin:'10001',coreInfo:{nick:'新版昵称',remark:'新版备注🙂'}}]}]),[{id:'10001',label:'新版备注🙂',nickname:'新版昵称'}]);
 assert.deepEqual(groups(0,[{groupCode:'30001',groupName:'群🙂',token:'secret'}]),[{id:'30001',label:'群🙂'}]);
 assert.equal(groups(4,[]),null);assert.equal(groups(2,[{groupCode:'30001'}]),null);
 assert.deepEqual(groups(1,[]),[]);
 assert.throws(()=>groups(4,[{groupCode:'30001'}]));
 assert.throws(()=>friends([{buddyList:[{uin:'wrong'}]}]));
 assert.throws(()=>friends([{buddyList:Array(10001).fill({uin:'10001'})}]));
 assert.equal(friends([{buddyList:[{uin:'10001',nick:'🙂'.repeat(200)}]}])[0].label.length,256);
});
function services(){
 let buddyListener,groupListener;
 const buddy={addKernelBuddyListener(fn){buddyListener=fn;},getBuddyList(){queueMicrotask(()=>buddyListener.onBuddyListChange([{buddyList:[{uin:'10001',nick:'测试'}]}]));}};
 const group={addKernelGroupListener(fn){groupListener=fn;},getGroupList(){queueMicrotask(()=>{
  groupListener.onGroupListUpdate(4,[]);groupListener.onGroupListUpdate(2,[{groupCode:'30002'}]);
  groupListener.onGroupListUpdate(0,[{groupCode:'30001',groupName:'测试群'}]);});}};
 return {getBuddyService:()=>buddy,getGroupService:()=>group};
}
test('read waits for snapshots without interpreting partial notifications as full lists',async()=>{
 const session=services();const result=await readDirectory(session,()=>{},100);
 assert.equal(result.private_targets[0].id,'10001');assert.equal(result.groups[0].id,'30001');
});
test('failed request and invalidated identity reject; late callbacks do not revive a result',async()=>{
 const session=services();session.getBuddyService().getBuddyList=()=>Promise.reject(Error('network'));
 await assert.rejects(readDirectory(session,()=>{},100),/network/);
 let checks=0;await assert.rejects(readDirectory(services(),()=>{if(++checks>1)throw Error('account changed');},100),/account changed/);
});
test('missing full snapshot is an explicit timeout, never an empty directory',async()=>{
 const session=services();session.getGroupService().getGroupList=()=>{};
 await assert.rejects(readDirectory(session,()=>{},10),/qq_native_catalog_invalid/);
});
