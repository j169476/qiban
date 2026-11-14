const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const persona={version:'v1',data:{name:'合成人物',core:{description:'原简介',speaking_style:['自然交流']},interests:['读书']}};
const digest='a'.repeat(64);
// Exactly the reply shapes the window maps for a retained local copy.
const preservation={name:'5f2a9c0d5e6b7a81ddccbackup.ccbackup',sha256:digest};
const copyA={handle:'synthetic-handle-a',name:'4f2a9c0d5e6b7a81ccbackup.ccbackup',
 taken_at:'2026-11-07T10:11:12+00:00',bytes:4096,usable:true};
const copyB={handle:'synthetic-handle-b',name:'3e1a2b3c4d5e6f7081.ccbackup',
 taken_at:'2026-11-06T08:00:00+00:00',bytes:0,usable:false};
const copyC={handle:'synthetic-handle-c',name:'2d3e4f5061728394aa.ccbackup',
 taken_at:'2026-11-05T07:30:00+00:00',bytes:2048,usable:true};
const previewResult={token:'synthetic-preserved-token',kind:'preserved',expires_in_seconds:600,
 records:7,history:2,name:copyA.name,taken_at:copyA.taken_at,
 source:{project:'synthetic-proj',role:'跨机恢复162-来源',installation:'synthetic-ins'},
 preservation,effect:'将完整替换当前资料与历史，两份资料不合并。'};
const switchedResult={switched:true,restart_required:true,preservation,
 message:'已从本机保全资料恢复完成，已停止旧后台。'};
function page(){
 const nodes=new Map(),listeners=[],requests=[],specials={};let sequence=0,locked=false;
 const create=()=>({textContent:'',value:'',children:[],dataset:{},disabled:false,hidden:false,open:false,
  append(...items){this.children.push(...items);},replaceChildren(){this.children=[];},showModal(){this.open=true;},close(){this.open=false;}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create());return nodes.get(id);};
 const replies={
  'session.status':()=>({locked,lock_enabled:true,room_session:'synthetic-session'}),
  'session.lock':()=>{locked=true;return {locked,lock_enabled:true};},
  'session.unlock':()=>{locked=false;return {locked,lock_enabled:true};},
  'config.read':()=>structuredClone(persona),'config.patch':()=>({version:'v2'}),
  'backups.list':()=>({backups:[]}),'runtime.status':()=>({platforms:[]}),'notifications.list':()=>({items:[],unread:0})
 };
 const webview={addEventListener(_,f){listeners.push(f);},
  postMessage(request){
   requests.push(request);
   Promise.resolve().then(async()=>{
    if(request.action&&request.action!=='management'){
     const handler=specials[request.action];
     if(!handler)throw new Error('未模拟的原生操作 '+request.action);
     return await handler(request);
    }
    const handler=replies[request.operation];
    if(!handler)throw new Error('未模拟的管理操作 '+request.operation);
    return {ok:true,result:await handler(request.arguments)};
   }).then(
    response=>listeners.forEach(f=>f({data:{type:'management',request_id:request.request_id,response}})),
    error=>listeners.forEach(f=>f({data:{type:'management',request_id:request.request_id,response:{ok:false,message:error.message}}})));
  }};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/management.js'),'utf8'),{
  document:{getElementById:node,createElement:create,createTextNode:text=>({textContent:text})},
  window:{chrome:{webview},dispatchEvent(){},addEventListener(){}},
  CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail;}},
  crypto:{randomUUID:()=>String(++sequence)},Date:{now:()=>10000},setTimeout:()=>sequence,clearTimeout(){}
 });
 const settle=async()=>{for(let round=0;round<3;round++)await new Promise(resolve=>setImmediate(resolve));};
 async function update(ready){listeners.forEach(f=>f({data:{type:'service',management:true,ready}}));await settle();}
 const special=(action,handler)=>{specials[action]=handler;};
 const native=()=>requests.filter(request=>request.action&&request.action!=='management');
 const options=()=>node('preserved-selection').children;
 const choose=handle=>{node('preserved-selection').value=handle;node('preserved-selection').onchange();};
 return {node,replies,requests,specials,special,native,options,choose,update,settle,
  lock:async()=>{node('lock-now').onclick();await settle();}};
}
test('listing shows each copy as a file name, file time and size, then pages with the cursor',async()=>{
 const p=page();
 p.special('portable-preserved-list',request=>request.after===''
  ?{ok:true,result:{items:[structuredClone(copyA)],next:'synthetic-cursor'}}
  :{ok:true,result:{items:[structuredClone(copyB)],next:''}});
 await p.update(true);
 await p.node('preserved-list').onclick();await p.settle();
 let sent=p.native();
 assert.equal(sent.length,1);assert.equal(sent[0].action,'portable-preserved-list');
 assert.deepEqual(Object.keys(sent[0]).sort(),['action','after','request_id']);
 assert.equal(sent[0].after,'');
 assert.equal(p.options().length,1);
 assert.match(p.options()[0].textContent,/4f2a9c0d5e6b7a81ccbackup\.ccbackup/);
 assert.match(p.options()[0].textContent,/2026-11-07 10:11:12/);
 assert.match(p.options()[0].textContent,/4 KB/);
 assert.equal(p.node('preserved-selection').value,'synthetic-handle-a');
 assert.equal(p.node('preserved-preview').disabled,false);
 assert.equal(p.node('preserved-more').disabled,false);
 await p.node('preserved-more').onclick();await p.settle();
 sent=p.native();
 assert.equal(sent.length,2);assert.deepEqual(Object.keys(sent[1]).sort(),['action','after','request_id']);
 assert.equal(sent[1].after,'synthetic-cursor');
 assert.equal(p.options().length,2);
 assert.match(p.options()[1].textContent,/无法读取/);
 assert.equal(p.options()[1].disabled,true);
 assert.equal(p.node('preserved-more').disabled,true);
});
test('an unusable copy can never be confirmed and no handle is selected for it',async()=>{
 const p=page();
 p.special('portable-preserved-list',()=>({ok:true,result:{items:[structuredClone(copyB)],next:''}}));
 await p.update(true);
 await p.node('preserved-list').onclick();await p.settle();
 assert.equal(p.options().length,1);assert.equal(p.options()[0].disabled,true);
 assert.equal(p.node('preserved-selection').value,'');
 assert.equal(p.node('preserved-preview').disabled,true);
 assert.equal(p.node('portable-restore').disabled,true);
});
test('preview sends only the handle and arms the same Windows confirmation',async()=>{
 const p=page();
 p.special('portable-preserved-list',()=>({ok:true,result:{items:[structuredClone(copyA)],next:''}}));
 p.special('portable-preserved-preview',()=>({ok:true,result:structuredClone(previewResult)}));
 p.special('portable-restore',()=>({ok:true,result:structuredClone(switchedResult)}));
 await p.update(true);
 await p.node('preserved-list').onclick();await p.settle();
 await p.node('preserved-preview').onclick();await p.settle();
 let sent=p.native();
 assert.equal(sent[1].action,'portable-preserved-preview');
 assert.deepEqual(Object.keys(sent[1]).sort(),['action','handle','request_id']);
 assert.equal(sent[1].handle,'synthetic-handle-a');
 assert.equal(sent[1].password,undefined);
 const source=p.node('portable-source').textContent;
 assert.match(source,/保全文件：4f2a9c0d5e6b7a81ccbackup\.ccbackup/);
 assert.match(source,/文件时间：2026-11-07 10:11:12/);
 assert.match(source,/7 条当前资料/);assert.match(source,/2 条历史/);
 assert.match(source,/跨机恢复162-来源/);
 assert.match(source,/另存为 5f2a9c0d5e6b7a81ddccbackup\.ccbackup/);
 assert.equal(source.includes('counters'),false);
 assert.equal(p.node('portable-restore').disabled,false);
 await p.node('portable-restore').onclick();await p.settle();
 sent=p.native();
 assert.equal(sent[2].action,'portable-restore');
 assert.deepEqual(Object.keys(sent[2]).sort(),['action','request_id','token']);
 assert.equal(sent[2].token,'synthetic-preserved-token');
 const message=p.node('portable-notice').textContent;
 assert.match(message,/已从本机保全资料恢复完成/);
 assert.match(message,/启动应用后台/);
 assert.match(message,/已保全为 5f2a9c0d5e6b7a81ddccbackup\.ccbackup/);
 assert.equal(p.node('feedback-title').textContent,'本机保全资料恢复完成');
 assert.equal(p.node('portable-restore').disabled,true);
 assert.equal(p.options().length,0);
 assert.equal(p.node('portable-source').textContent,'');
});
test('choosing another copy clears the armed preview and requires a new one',async()=>{
 const p=page();
 p.special('portable-preserved-list',()=>({ok:true,result:{items:[structuredClone(copyA),structuredClone(copyC)],next:''}}));
 p.special('portable-preserved-preview',()=>({ok:true,result:structuredClone(previewResult)}));
 p.special('portable-restore',()=>({ok:true,result:structuredClone(switchedResult)}));
 await p.update(true);
 await p.node('preserved-list').onclick();await p.settle();
 await p.node('preserved-preview').onclick();await p.settle();
 assert.equal(p.node('portable-restore').disabled,false);
 assert.match(p.node('portable-source').textContent,/保全文件：4f2a9c0d5e6b7a81ccbackup\.ccbackup/);
 const previews=p.native().filter(request=>request.action==='portable-preserved-preview').length;
 // The user selects the other copy: the confirmation must not stay armed for the first one.
 p.choose('synthetic-handle-c');
 assert.equal(p.node('portable-restore').disabled,true);
 assert.equal(p.node('portable-source').textContent,'');
 assert.match(p.node('portable-notice').textContent,/重新查看/);
 await p.node('portable-restore').onclick();await p.settle();
 assert.equal(p.native().filter(request=>request.action==='portable-restore').length,0);
 // Previewing the newly chosen copy arms the same confirmation again.
 await p.node('preserved-preview').onclick();await p.settle();
 const requests=p.native();
 assert.equal(requests.filter(request=>request.action==='portable-preserved-preview').length,previews+1);
 assert.equal(requests.filter(request=>request.action==='portable-preserved-preview').pop().handle,'synthetic-handle-c');
 assert.equal(p.node('portable-restore').disabled,false);
 await p.node('portable-restore').onclick();await p.settle();
 assert.equal(p.native().filter(request=>request.action==='portable-restore').length,1);
});
test('a refused preview keeps the confirmation disabled and explains why',async()=>{
 const p=page();
 p.special('portable-preserved-list',()=>({ok:true,result:{items:[structuredClone(copyA)],next:''}}));
 p.special('portable-preserved-preview',()=>({ok:false,error:'preserved_source',
  message:'这份本机保全资料不属于当前安装，无法在本机恢复'}));
 await p.update(true);
 await p.node('preserved-list').onclick();await p.settle();
 await p.node('preserved-preview').onclick();await p.settle();
 assert.match(p.node('feedback-message').textContent,/不属于当前安装/);
 assert.equal(p.node('portable-restore').disabled,true);
 assert.equal(p.node('portable-source').textContent,'');
});
test('a cancelled Windows confirmation reports a refusal and no restart',async()=>{
 const p=page();
 p.special('portable-preserved-list',()=>({ok:true,result:{items:[structuredClone(copyA)],next:''}}));
 p.special('portable-preserved-preview',()=>({ok:true,result:structuredClone(previewResult)}));
 p.special('portable-restore',()=>({ok:false,error:'windows_verification',
  message:'已取消 Windows 验证，当前资料未改变。请重新查看预览后再恢复。'}));
 await p.update(true);
 await p.node('preserved-list').onclick();await p.settle();
 await p.node('preserved-preview').onclick();await p.settle();
 await p.node('portable-restore').onclick();await p.settle();
 assert.equal(p.node('feedback-title').textContent,'本机保全资料恢复未完成');
 const message=p.node('feedback-message').textContent;
 assert.match(message,/未改变/);
 assert.equal(message.includes('启动应用后台'),false);
 assert.equal(p.node('portable-restore').disabled,true);
 assert.equal(p.native().filter(request=>request.action==='portable-restore').length,1);
});
test('locking clears the listing and drops a late listing reply',async()=>{
 const p=page();let release;
 p.special('portable-preserved-list',()=>new Promise(resolve=>{release=resolve;}));
 await p.update(true);
 const pending=p.node('preserved-list').onclick();await p.settle();
 await p.lock();
 release({ok:true,result:{items:[structuredClone(copyA)],next:'synthetic-cursor'}});
 await pending;await p.settle();
 assert.equal(p.options().length,0);
 assert.equal(p.node('preserved-preview').disabled,true);
 assert.equal(p.node('preserved-more').disabled,true);
 assert.equal(p.native().length,1);
 assert.equal(p.node('management-panel').dataset.locked,'true');
});
test('no preserved request carries a path, a password or an unlisted key',async()=>{
 const p=page();
 p.special('portable-preserved-list',request=>request.after===''
  ?{ok:true,result:{items:[structuredClone(copyA)],next:'synthetic-cursor'}}
  :{ok:true,result:{items:[],next:''}});
 p.special('portable-preserved-preview',()=>({ok:true,result:structuredClone(previewResult)}));
 await p.update(true);
 await p.node('preserved-list').onclick();await p.settle();
 await p.node('preserved-more').onclick();await p.settle();
 p.choose('synthetic-handle-a');
 await p.node('preserved-preview').onclick();await p.settle();
 const allowed=[['action','after','request_id'],['action','handle','request_id']];
 for(const request of p.native()){
  const keys=Object.keys(request).sort();
  assert.ok(allowed.some(names=>JSON.stringify(names)===JSON.stringify(keys)),'意外的请求字段：'+keys.join(','));
  const text=JSON.stringify(request);
  assert.equal(text.includes('\\\\'),false);
  assert.equal(text.includes('.ccbackup'),false);
  assert.equal(text.includes('password'),false);
  assert.equal(text.includes('generation'),false);
 }
});
// The backups panel reads the same desktop-last-restore receipt a switch commits. A portable
// or preserved restore records the source it restored in `source_name` and, separately, the
// copy of this machine's own data taken just before the switch in `preservation_name`.
test('the backups notice names the restored source and keeps the preservation copy separate',async()=>{
 const cases=[{source:'portable',source_name:'qiban-便携备份-20261107.ccbackup'},
  {source:'preserved',source_name:copyA.name}];
 for(const item of cases){
  const p=page();
  p.replies['backups.list']=()=>({backups:[],last_restore:{id:'synthetic-restore-receipt',
   status:'complete',source:item.source,source_name:item.source_name,
   preservation_name:preservation.name,completed_at:'2026-11-08T09:00:00+00:00',
   role_started:false,generation:'f'.repeat(32)}});
  await p.update(true);
  // The source is what was restored; the pre-restore copy is only ever named as that copy.
  assert.equal(p.node('backup-notice').textContent,
   `上次恢复已完成：${item.source_name}；恢复前资料已保全为 ${preservation.name}。角色保持停止。`);
 }
});
// Neither an older receipt (which kept only the pre-restore copy in `name`) nor a new one
// with an unnamed source may present that copy as the source: the source stays unrecorded.
test('a receipt without a recorded source never reuses the preservation copy',async()=>{
 const p=page();
 p.replies['backups.list']=()=>({backups:[],last_restore:{id:'legacy-receipt',status:'complete',
  source:'preserved',name:preservation.name,completed_at:'2026-11-08T09:00:00+00:00',
  role_started:false}});
 await p.update(true);
 const unrecorded=`上次恢复已完成：来源未标注；恢复前资料已保全为 ${preservation.name}。角色保持停止。`;
 assert.equal(p.node('backup-notice').textContent,unrecorded);
 // A new receipt whose host supplied no source name says exactly the same thing.
 p.replies['backups.list']=()=>({backups:[],last_restore:{id:'new-receipt',status:'complete',
  source:'portable',source_name:null,preservation_name:preservation.name,
  completed_at:'2026-11-08T09:00:00+00:00',role_started:false}});
 await p.node('backup-refresh').onclick();await p.settle();
 assert.equal(p.node('backup-notice').textContent,unrecorded);
 // An unfinished restore never names any file at all.
 p.replies['backups.list']=()=>({backups:[],last_restore:{id:'pending-receipt',
  status:'assets_pending',source:'portable',source_name:null,
  preservation_name:preservation.name,completed_at:'2026-11-08T09:00:00+00:00',role_started:false}});
 await p.node('backup-refresh').onclick();await p.settle();
 assert.equal(p.node('backup-notice').textContent,'资料已恢复，正在补齐登记图片；完成前不能运行角色。');
});
// A local backup restore keeps its own meaning: `name` is the backup the user chose to restore.
test('a local backup restore still names the backup the user chose',async()=>{
 const p=page();
 const chosen='local-chosen.ccbackup';
 const receipt={id:'local-receipt',status:'complete',name:chosen,
  completed_at:'2026-11-08T09:00:00+00:00',role_started:false};
 p.replies['backups.list']=()=>({backups:[{name:chosen,bytes:2048}],last_restore:structuredClone(receipt)});
 p.replies['backups.preview']=()=>({token:'synthetic-local-token',name:chosen,
  expires_in_seconds:180,records:5,effect:'将覆盖当前资料和历史记录。'});
 p.special('restore-backup',()=>({ok:true,result:{last_restore:structuredClone(receipt),restored:true}}));
 await p.update(true);
 assert.equal(p.node('backup-notice').textContent,`上次恢复已完成：${chosen}。角色保持停止。`);
 p.node('backup-selection').value=chosen;
 await p.node('backup-preview').onclick();await p.settle();
 await p.node('backup-restore').onclick();await p.settle();
 assert.equal(p.node('feedback-title').textContent,'资料恢复完成');
 assert.equal(p.node('feedback-message').textContent,
  `已从 ${chosen} 恢复资料，当前应用密码锁保持不变。角色保持停止，请重新检查平台授权。`);
});
