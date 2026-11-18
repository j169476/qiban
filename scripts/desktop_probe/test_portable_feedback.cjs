const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const persona={version:'v1',data:{name:'合成人物',core:{description:'原简介',speaking_style:['自然交流']},interests:['读书']}};
const digest='a'.repeat(64);
// Exactly the reply shapes src/qiban/desktop_portable.py returns.
const preservation={name:'4f2a9c0d5e6b7a81ccbackup.ccbackup',sha256:digest};
const exportResult={backup:'qiban-便携备份-20261107.ccbackup',key_file:'qiban-便携备份-20261107.cckey',
 backup_bytes:4096,key_bytes:300,backup_sha256:digest,key_sha256:digest,records:12,history:34,encrypted:true};
const previewResult={token:'synthetic-preview-token',expires_in_seconds:600,records:12,history:34,
 counters:{current_records:12,history_records:34,encrypted_records:120,ledger_envelopes:47,assets:3},
 source:{project:'synthetic-proj',role:'合成角色',installation:'synthetic-ins'},
 preservation,effect:'将完整替换当前资料与历史，两份资料不合并。'};
const switchedResult={switched:true,restart_required:true,preservation,backend:'stopped',
 message:'便携恢复已切换完成，已停止旧后台。'};
function page(){
 const nodes=new Map(),listeners=[],requests=[],specials={},events={},body={dataset:{tabBackup:'portable'}};
 let sequence=0,locked=false;
 const create=()=>({textContent:'',value:'',children:[],dataset:{},disabled:false,hidden:false,open:false,
  append(...items){this.children.push(...items);},replaceChildren(){this.children=[];},showModal(){this.open=true;},close(){this.open=false;}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create());return nodes.get(id);};
 const replies={
  'session.status':()=>({locked,lock_enabled:true,room_session:'synthetic-session'}),
  'session.lock':()=>{locked=true;return {locked,lock_enabled:true};},
  'session.unlock':()=>{locked=false;return {locked,lock_enabled:true};},
  'config.read':()=>structuredClone(persona),'config.patch':()=>({version:'v2'}),
  'backups.list':()=>({backups:[]}),'runtime.status':()=>({platforms:[]}),'notifications.page':()=>({items:[],unread:0,total:0,page:1,pages:1})
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
  document:{body,getElementById:node,createElement:create,createTextNode:text=>({textContent:text})},
  window:{chrome:{webview},dispatchEvent(){},addEventListener:(name,handler)=>{events[name]=handler;}},
  CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail;}},
  crypto:{randomUUID:()=>String(++sequence)},Date:{now:()=>10000},setTimeout:()=>sequence,clearTimeout(){}
 });
 const settle=async()=>{for(let round=0;round<3;round++)await new Promise(resolve=>setImmediate(resolve));};
 async function update(ready){listeners.forEach(f=>f({data:{type:'service',management:true,ready}}));await settle();}
 const special=(action,handler)=>{specials[action]=handler;};
 const portable=()=>requests.filter(request=>request.action&&request.action!=='management');
 const fill=(password,confirm)=>{node('portable-password').value=password;node('portable-confirm').value=confirm??password;};
 function selectSource(target){
  body.dataset.tabBackup=target;
  events['qiban-tab-change']({detail:{group:'backup',target}});
 }
 return {node,replies,requests,specials,special,portable,fill,update,settle,selectSource,
  lock:async()=>{node('lock-now').onclick();await settle();}};
}
const password='Synthetic-Portable-Recovery-2026';
test('switching to a different backup source hides and disarms the previous confirmation',async()=>{
 const p=page();p.special('portable-preview',()=>({ok:true,result:previewResult}));
 p.special('portable-cancel',()=>({ok:true,result:{cancelled:true}}));
 await p.update(true);p.fill(password);await p.node('portable-import').onclick();
 assert.equal(p.node('portable-preview-view').hidden,false);
 p.selectSource('preserved');
 assert.equal(p.node('portable-preview-view').hidden,true);
 assert.equal(p.node('portable-empty').hidden,false);
 await p.node('portable-restore').onclick();
 await p.node('portable-cancel').onclick();
 assert.deepEqual(p.portable().map(request=>request.action),['portable-preview']);
 p.selectSource('portable');
 assert.equal(p.node('portable-preview-view').hidden,false);
 assert.equal(p.node('portable-cancel').disabled,false);
 await p.node('portable-cancel').onclick();
 assert.deepEqual(p.portable().map(request=>request.action),['portable-preview','portable-cancel']);
});
test('export shows the real pair names, sizes and counts and clears both fields',async()=>{
 const p=page();
 p.special('portable-export',()=>({ok:true,result:structuredClone(exportResult)}));
 await p.update(true);
 p.fill(password);
 const run=p.node('portable-export').onclick();await p.settle();
 assert.equal(p.node('portable-password').value,'');assert.equal(p.node('portable-confirm').value,'');
 await run;
 const sent=p.portable();
 assert.equal(sent.length,1);assert.equal(sent[0].action,'portable-export');
 assert.deepEqual(Object.keys(sent[0]).sort(),['action','password','request_id']);
 assert.equal(sent[0].password,password);
 const message=p.node('feedback-message').textContent;
 assert.match(message,/qiban-便携备份-20261107\.ccbackup/);
 assert.match(message,/qiban-便携备份-20261107\.cckey/);
 assert.match(message,/4 KB/);
 assert.match(message,/12 条当前资料/);assert.match(message,/34 条历史/);
 assert.equal(p.node('management-feedback').open,true);
});
test('export refuses a short or mismatched recovery password without any request',async()=>{
 const p=page();await p.update(true);
 p.fill('short','short');await p.node('portable-export').onclick();await p.settle();
 assert.equal(p.portable().length,0);assert.match(p.node('feedback-message').textContent,/16/);
 p.fill(password,'another different password');await p.node('portable-export').onclick();await p.settle();
 assert.equal(p.portable().length,0);assert.match(p.node('feedback-message').textContent,/不一致/);
 assert.equal(p.node('portable-password').value,'');
});
test('preview shows the source labels, both counts and the preservation backup',async()=>{
 const p=page();
 p.special('portable-preview',()=>({ok:true,result:structuredClone(previewResult)}));
 await p.update(true);
 p.fill(password);await p.node('portable-import').onclick();await p.settle();
 const sent=p.portable();
 assert.equal(sent.length,1);assert.equal(sent[0].action,'portable-preview');
 assert.deepEqual(Object.keys(sent[0]).sort(),['action','password','request_id']);
 const source=p.node('portable-source').textContent;
 assert.match(source,/来源角色：合成角色/);assert.match(source,/synthetic-proj/);assert.match(source,/synthetic-ins/);
 assert.match(source,/12 条当前资料/);assert.match(source,/34 条历史/);
 assert.match(source,/不与当前资料合并/);assert.match(source,/应用密码锁/);
 assert.match(source,/已保全为 4f2a9c0d5e6b7a81ccbackup\.ccbackup/);
 // Encrypted-store counters are implementation detail and never reach the page.
 assert.equal(source.includes('counters'),false);
 assert.equal(source.includes('encrypted'),false);
 assert.equal(source.includes('120'),false);assert.equal(source.includes('47'),false);
 assert.equal(p.node('portable-restore').disabled,false);
 assert.equal(p.node('portable-cancel').disabled,false);
 assert.equal(p.node('portable-password').value,'');
});
test('cancelled file selection changes nothing and keeps the confirm button disabled',async()=>{
 const p=page();
 p.special('portable-preview',()=>({ok:false,error:'cancelled',message:'已取消导入预览，当前资料未改变。'}));
 await p.update(true);p.fill(password);await p.node('portable-import').onclick();await p.settle();
 assert.match(p.node('feedback-message').textContent,/未改变/);
 assert.equal(p.node('portable-restore').disabled,true);
 assert.equal(p.node('portable-password').value,'');
});
test('a locked session never opens the picker',async()=>{
 const p=page();
 p.special('portable-export',()=>({ok:false,error:'desktop_locked',message:'栖伴已锁定，请先解锁再导出便携备份。'}));
 await p.update(true);p.fill(password);await p.node('portable-export').onclick();await p.settle();
 assert.match(p.node('feedback-message').textContent,/已锁定/);
 assert.equal(p.portable().length,1);
});
test('confirmation sends the token only and reports the stopped backend',async()=>{
 const p=page();
 p.special('portable-preview',()=>({ok:true,result:structuredClone(previewResult)}));
 p.special('portable-restore',()=>({ok:true,result:structuredClone(switchedResult)}));
 await p.update(true);p.fill(password);await p.node('portable-import').onclick();await p.settle();
 await p.node('portable-restore').onclick();await p.settle();
 const sent=p.portable();
 assert.equal(sent.length,2);assert.equal(sent[1].action,'portable-restore');
 assert.deepEqual(Object.keys(sent[1]).sort(),['action','request_id','token']);
 assert.equal(sent[1].token,'synthetic-preview-token');assert.equal(sent[1].password,undefined);
 const message=p.node('portable-notice').textContent;
 assert.match(message,/启动应用后台/);
 assert.match(message,/已保全为 4f2a9c0d5e6b7a81ccbackup\.ccbackup/);
 assert.equal(message.includes('启动验证服务'),false);
 assert.equal(p.node('feedback-title').textContent,'便携恢复完成');
 assert.equal(p.node('portable-restore').disabled,true);
 assert.equal(p.node('portable-source').textContent,'');
});
test('an unknown switch outcome asks for a restart check and never retries',async()=>{
 const p=page();
 p.special('portable-preview',()=>({ok:true,result:structuredClone(previewResult)}));
 p.special('portable-restore',()=>({ok:true,result:{switched:false,restart_required:true,preservation,
  backend:'stopped',message:'恢复结果未知：切换步骤可能已提交。'}}));
 await p.update(true);p.fill(password);await p.node('portable-import').onclick();await p.settle();
 await p.node('portable-restore').onclick();await p.settle();
 assert.equal(p.node('feedback-title').textContent,'便携恢复结果未知');
 const message=p.node('feedback-message').textContent;
 assert.match(message,/启动应用后台/);assert.equal(message.includes('启动验证服务'),false);
 assert.match(message,/不会重复提交/);assert.match(message,/核对资料/);
 assert.equal(/失败|重试/.test(message),false);
 assert.equal(p.portable().filter(request=>request.action==='portable-restore').length,1);
 await p.node('portable-restore').onclick();await p.settle();
 assert.equal(p.portable().filter(request=>request.action==='portable-restore').length,1);
});
test('a pre-switch refusal invites a new preview instead of a restart',async()=>{
 const p=page();
 p.special('portable-preview',()=>({ok:true,result:structuredClone(previewResult)}));
 p.special('portable-restore',()=>({ok:false,error:'portable_verification_expired',
  message:'便携恢复验证已失效，请重新预览并验证 Windows 身份'}));
 await p.update(true);p.fill(password);await p.node('portable-import').onclick();await p.settle();
 await p.node('portable-restore').onclick();await p.settle();
 assert.equal(p.node('feedback-title').textContent,'便携恢复未完成');
 assert.match(p.node('feedback-message').textContent,/已失效/);
 assert.equal(p.node('portable-restore').disabled,true);
});
test('cancel sends no token and leaves the confirm button disabled',async()=>{
 const p=page();
 p.special('portable-preview',()=>({ok:true,result:structuredClone(previewResult)}));
 p.special('portable-cancel',()=>({ok:true,result:{cancelled:true}}));
 await p.update(true);p.fill(password);await p.node('portable-import').onclick();await p.settle();
 await p.node('portable-cancel').onclick();await p.settle();
 const sent=p.portable();
 assert.equal(sent.length,2);assert.equal(sent[1].action,'portable-cancel');
 assert.deepEqual(Object.keys(sent[1]).sort(),['action','request_id']);
 assert.match(p.node('portable-notice').textContent,/未改变/);
 assert.equal(p.node('portable-restore').disabled,true);
});
test('repeated clicks while busy post one request',async()=>{
 const p=page();let release;
 p.special('portable-export',()=>new Promise(resolve=>{release=resolve;}));
 await p.update(true);p.fill(password);
 const first=p.node('portable-export').onclick();await p.settle();
 const second=p.node('portable-export').onclick();await p.settle();
 assert.equal(p.portable().length,1);
 release({ok:true,result:structuredClone(exportResult)});
 await first;await second;await p.settle();
});
test('disconnect clears the preview and drops a late preview reply',async()=>{
 const p=page();let release;
 p.special('portable-preview',()=>new Promise(resolve=>{release=resolve;}));
 await p.update(true);p.fill(password);
 const pending=p.node('portable-import').onclick();await p.settle();
 await p.update(false);
 release({ok:true,result:structuredClone(previewResult)});await pending;await p.settle();
 assert.equal(p.node('portable-source').textContent,'');
 assert.equal(p.node('portable-restore').disabled,true);
 assert.equal(p.node('portable-password').value,'');
 assert.equal(p.node('portable-confirm').value,'');
});
test('lock while a preview is open clears it and refuses the late reply',async()=>{
 const p=page();let release;
 p.special('portable-preview',()=>new Promise(resolve=>{release=resolve;}));
 await p.update(true);p.fill(password);
 const pending=p.node('portable-import').onclick();await p.settle();
 await p.lock();
 release({ok:true,result:structuredClone(previewResult)});await pending;await p.settle();
 assert.equal(p.node('portable-source').textContent,'');
 assert.equal(p.node('portable-restore').disabled,true);
 assert.equal(p.node('management-panel').dataset.locked,'true');
});
test('no page request ever carries a path or an unlisted key',async()=>{
 const p=page();
 p.special('portable-export',()=>({ok:true,result:structuredClone(exportResult)}));
 p.special('portable-preview',()=>({ok:true,result:structuredClone(previewResult)}));
 p.special('portable-restore',()=>({ok:true,result:structuredClone(switchedResult)}));
 p.special('portable-cancel',()=>({ok:true,result:{cancelled:true}}));
 await p.update(true);
 p.fill(password);await p.node('portable-export').onclick();await p.settle();
 p.fill(password);await p.node('portable-import').onclick();await p.settle();
 await p.node('portable-cancel').onclick();await p.settle();
 p.fill(password);await p.node('portable-import').onclick();await p.settle();
 await p.node('portable-restore').onclick();await p.settle();
 const allowed=[['action','password','request_id'],['action','request_id','token'],['action','request_id']];
 for(const request of p.portable()){
  const keys=Object.keys(request).sort();
  assert.ok(allowed.some(names=>JSON.stringify(names)===JSON.stringify(keys)),'意外的请求字段：'+keys.join(','));
  assert.equal(JSON.stringify(request).includes('\\\\'),false);
  assert.equal(JSON.stringify(request).includes('.ccbackup'),false);
  assert.equal(JSON.stringify(request).includes('.cckey'),false);
 }
});
