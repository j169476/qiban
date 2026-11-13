// Reply quota page tests use only synthetic replies; nothing here reaches a platform.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');

const DEFAULTS={private_daily_replies:200,group_session_replies:5,
 group_session_decisions:20,group_session_minutes:10};
const BOUNDS={private_daily_replies:[0,10000],group_session_replies:[1,100],
 group_session_decisions:[1,1000],group_session_minutes:[1,120]};
const FIELDS={private_daily_replies:'私聊每自然日回复数',group_session_replies:'群参与每阶段回复数',
 group_session_decisions:'群参与每阶段决策数',group_session_minutes:'群参与阶段时长（分钟）'};

function page(){
 const nodes=new Map(),events={},requests=[],listeners=[],timers=new Map();
 const element=tag=>({tagName:tag,textContent:'',hidden:false,disabled:false,value:'',className:'',
  type:'',min:'',max:'',step:'',attributes:{},children:[],
  append(...kids){for(const kid of kids)this.children.push(kid);},
  replaceChildren(...kids){this.children=kids;this.value='';},
  setAttribute(name,value){this.attributes[name]=value;},
  removeAttribute(name){delete this.attributes[name];}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,element('div'));return nodes.get(id);};
 let sequence=0;
 const webview={addEventListener(_,fn){listeners.push(fn);},postMessage(request){requests.push(request);}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/reply_quota.js'),'utf8'),{
  document:{getElementById:node,createElement:element},
  window:{chrome:{webview},addEventListener(name,fn){events[name]=fn;}},
  crypto:{randomUUID:()=>String(++sequence)},
  setTimeout(fn){timers.set(++sequence,fn);return sequence;},
  clearTimeout(id){timers.delete(id);},
  Date,Number,Error,Map,Set,Array,Object,String,JSON,Promise});
 const settle=()=>new Promise(resolve=>setImmediate(resolve));
 async function flush(count){for(let index=0;index<(count||6);index++)await settle();}
 // Only a ready, unlocked backend may issue a request; the first unlock loads the summary.
 function state(ready,unlocked){events['qiban-management-state']({detail:{ready,locked:!unlocked}});}
 async function reply(request,result){
  for(const fn of listeners)fn({data:{type:'management',request_id:request.request_id,
   response:{ok:true,result}}});
  await settle();
 }
 async function fail(request,error,message){
  for(const fn of listeners)fn({data:{type:'management',request_id:request.request_id,
   response:{ok:false,error,message}}});
  await settle();
 }
 function fireTimers(){
  const pending=Array.from(timers.entries());
  timers.clear();
  for(const [,fn] of pending)fn();
 }
 function walk(target,visit){for(const kid of target.children){visit(kid);walk(kid,visit);}}
 function texts(target){const out=[];walk(target,n=>{if(n.textContent)out.push(n.textContent);});return out;}
 function buttons(target){
  const found=[];walk(target,n=>{if(n.tagName==='button')found.push(n);});return found;
 }
 function button(target,text){
  const found=buttons(target).find(node=>node.textContent===text);
  assert.ok(found,'missing button '+text+' in '+JSON.stringify(texts(target)));
  return found;
 }
 function inputs(target){
  const found=[];walk(target,n=>{if(n.tagName==='input')found.push(n);});return found;
 }
 function tags(target,name){
  const found=[];walk(target,n=>{if(n.tagName===name)found.push(n);});return found;
 }
 return {node,requests,state,reply,fail,settle,flush,fireTimers,texts,buttons,button,inputs,tags};
}

function account(overrides={}){
 return {key:'qq:10001',platform:'qq',account:'10001',label:'合成账号',selected:true,version:null,
  settings:{...DEFAULTS},registered:2,manual:2,eligible:2,allow_all:{private:false,group:false},
  platform_running:false,...overrides};
}

function summary(accounts,overrides={}){
 const rows=accounts||[account()];
 return {accounts:rows,total:overrides.total===undefined?rows.length:overrides.total,
  offset:overrides.offset||0,next:overrides.next===undefined?null:overrides.next,
  bounds:BOUNDS,defaults:DEFAULTS,fields:FIELDS,
  private_fields:['private_daily_replies'],
  group_fields:['group_session_replies','group_session_decisions','group_session_minutes'],
  timezone:'Asia/Shanghai',day:'2026-11-09',quota_scope:'contact_calendar_day',billing:false};
}

function privateRow(overrides={}){
 return {scope_id:'reply-scope-v1-private',kind:'private',target:'10002',name:null,
  label:'私聊 · 10002',authorized:true,manual:true,member_present:true,allow_all:false,registered:true,
  status:'当前有效：已授权回复该会话',account:'10001',platform:'qq',
  settings:{private_daily_replies:200},inherited:true,version:'private-v1',
  fields:['private_daily_replies'],limit:200,
  usage:{confirmed:2,reserved:1,uncertain:1},used:4,...overrides};
}

function groupRow(overrides={}){
 return {scope_id:'reply-scope-v1-group',kind:'group',target:'10003',name:'合成群',
  label:'群 · 合成群',authorized:true,manual:true,member_present:true,allow_all:false,registered:true,
  status:'当前有效：已授权回复该会话',account:'10001',platform:'qq',
  settings:{group_session_replies:5,group_session_decisions:20,group_session_minutes:10},
  inherited:false,version:'group-v1',
  fields:['group_session_replies','group_session_decisions','group_session_minutes'],
  window:{current:true,note:'当前阶段进行中',id:'stage-1',status:'active',started:1,expires:0,
   reply_limit:5,decision_limit:20,decisions:2,used:1,confirmed:1,reserved:0,uncertain:0},...overrides};
}

function conversations(items,overrides={}){
 return {platform:'qq',account:'10001',label:'合成账号',items,total:items.length,offset:0,
  next:null,previous:null,...overrides};
}

async function unlocked(payload){
 const p=page();
 p.state(true,true);
 assert.equal(p.requests.at(-1).operation,'reply_quota.summary');
 await p.reply(p.requests.at(-1),payload||summary());
 return p;
}

test('nothing is requested before unlock and a reply arriving after lock cannot repaint',async()=>{
 const p=page();
 p.state(false,false);
 p.state(true,false);
 assert.equal(p.requests.length,0);
 p.state(true,true);
 const request=p.requests.at(-1);
 assert.equal(request.operation,'reply_quota.summary');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),{after:''});
 p.state(true,false);
 await p.reply(request,summary());
 assert.equal(p.node('reply-quota-accounts').children.length,0);
 assert.equal(p.node('reply-quota-rule').textContent,'');
 p.state(true,true);
 assert.equal(p.requests.at(-1).operation,'reply_quota.summary');
});

test('a failed first read, a retry and a timeout are all visible',async()=>{
 const p=page();
 p.state(true,true);
 await p.fail(p.requests.at(-1),'database_error','数据库操作未完成；请检查本机数据库状态与版本冲突');
 assert.match(p.node('reply-quota-status').textContent,/数据库操作未完成/);
 assert.equal(p.node('reply-quota-accounts').children.length,0);

 const job=p.node('reply-quota-refresh').onclick();
 const request=p.requests.at(-1);
 assert.equal(request.operation,'reply_quota.summary');
 p.fireTimers();
 await job;
 assert.match(p.node('reply-quota-status').textContent,/操作超时，请重新读取回复额度。/);

 const retry=p.node('reply-quota-refresh').onclick();
 const again=p.requests.at(-1);
 await p.reply(again,summary());
 await retry;
 assert.match(p.node('reply-quota-status').textContent,/已读取 1\/1 个账号/);
 assert.equal(p.node('reply-quota-accounts').children.length,1);
});

test('account defaults and conversations are told apart and both save with one button',async()=>{
 const p=await unlocked();
 // Every render rebuilds the panel, so always read the live block again.
 const block=()=>p.node('reply-quota-accounts').children[0];
 assert.match(p.texts(block()).join('|'),/QQ · 10001（合成账号） · 当前使用/);
 assert.match(p.texts(block()).join('|'),/账号默认额度（影响所有未单独设置的会话）/);
 assert.match(p.texts(block()).join('|'),/已登记会话 2 个，当前有效 2 个，手动勾选 2 个。/);
 // The running flag belongs to the platform, never to the historical account.
 assert.match(p.texts(block()).join('|'),/QQ平台当前未运行；额度可以离线保存，下次运行按已保存的额度执行。/);
 assert.equal(p.inputs(block()).length,4);
 assert.deepEqual(p.inputs(block()).map(node=>node.value),['200','5','20','10']);
 assert.match(p.node('reply-quota-rule').textContent,/保存设置不会调用模型；这不是计费额度。/);

 for(const value of ['','1.5',' 3','+3','1e3','-1','200.0']){
  p.inputs(block())[0].value=value;
  p.inputs(block())[0].oninput();
  p.button(block(),'保存更改').onclick();
  await p.flush(2);
  assert.equal(p.requests.at(-1).operation,'reply_quota.summary','rejected '+JSON.stringify(value));
  assert.match(p.texts(block()).join('|'),/不能留空|必须是|必须在|0 或正整数/);
 }
 p.inputs(block())[0].value='10001';
 p.button(block(),'保存更改').onclick();
 await p.flush(2);
 assert.match(p.texts(block()).join('|'),/私聊每自然日回复数必须在 0 至 10000 之间。/);

 p.inputs(block())[0].value='120';p.inputs(block())[1].value='3';
 const job=p.button(block(),'保存更改').onclick();
 const request=p.requests.at(-1);
 assert.equal(request.operation,'reply_quota.account_save');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {platform:'qq',account:'10001',expected:null,
   changes:{private_daily_replies:120,group_session_replies:3,group_session_decisions:20,
    group_session_minutes:10}});
 // While a save is in flight the fields are locked and a second click cannot write.
 await p.flush(2);
 assert.equal(p.inputs(block())[0].disabled,true);
 assert.equal(p.button(block(),'保存更改').disabled,true);
 const before=p.requests.length;
 p.button(block(),'保存更改').onclick();
 await p.flush(2);
 assert.equal(p.requests.length,before);
 await p.reply(request,{platform:'qq',account:'10001',version:'account-v2',changed:true,
  settings:{...DEFAULTS,private_daily_replies:120,group_session_replies:3}});
 await job;
 assert.match(p.texts(block()).join('|'),/账号默认额度已保存；只影响继承账号默认的会话，单独设置的会话不受影响。/);
 assert.deepEqual(p.inputs(block()).map(node=>node.value),['120','3','20','10']);
 assert.equal(p.inputs(block())[0].disabled,false);
 p.button(block(),'保存更改').onclick();
 assert.equal(p.requests.at(-1).arguments.expected,'account-v2');
});

test('conversation rows show authorization, inheritance, natural-day usage and stage state',async()=>{
 const p=await unlocked();
 const block=p.node('reply-quota-accounts').children[0];
 const open=p.button(block,'查看已登记会话').onclick();
 const request=p.requests.at(-1);
 assert.equal(request.operation,'reply_quota.scopes');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {platform:'qq',account:'10001',after:''});
 await p.reply(request,conversations([privateRow({
   authorized:false,manual:false,status:'已撤销：不会回复该会话（仍可调整任何额度）'}),groupRow()]));
 await open;
 const scopeTexts=p.texts(p.node('reply-quota-accounts')).join('|');
 assert.match(scopeTexts,/单会话额度：已登记会话共 2 个，本页显示第 1 至 2 个/);
 assert.match(scopeTexts,/私聊 · 10002/);
 assert.match(scopeTexts,/授权状态：已撤销：不会回复该会话（仍可调整任何额度）/);
 assert.match(scopeTexts,/授权状态：当前有效：已授权回复该会话/);
 assert.match(scopeTexts,/额度：继承账号默认/);
 assert.match(scopeTexts,/额度：单独设置/);
 assert.match(scopeTexts,/私聊今日（2026-11-09，Asia\/Shanghai）：已确认 2 条，预留 1 条，未确认 1 条；当前上限 200 条。/);
 assert.match(scopeTexts,/群参与阶段：当前阶段进行中。本阶段已用 1\/5 条，决策 2\/20 次/);
 const articles=p.tags(p.node('reply-quota-accounts'),'article');
 // Nothing can be restored on a conversation that already inherits.
 assert.equal(p.button(articles[0],'恢复账号默认').disabled,true);
 assert.equal(p.button(articles[1],'恢复账号默认').disabled,false);
});

test('saving one conversation keeps its feedback next to that conversation only',async()=>{
 const p=await unlocked();
 const articles=()=>p.tags(p.node('reply-quota-accounts'),'article');
 const open=p.button(p.node('reply-quota-accounts').children[0],'查看已登记会话').onclick();
 await p.reply(p.requests.at(-1),conversations([privateRow(),groupRow()]));
 await open;
 assert.equal(articles().length,2);
 p.inputs(articles()[0])[0].value='9';
 const job=p.button(articles()[0],'保存更改').onclick();
 const request=p.requests.at(-1);
 assert.equal(request.operation,'reply_quota.scope_save');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {scope_id:'reply-scope-v1-private',account:'10001',expected:'private-v1',
   changes:{private_daily_replies:9}});
 await p.reply(request,privateRow({changed:true,inherited:false,version:'private-v2',
  settings:{private_daily_replies:9},limit:9}));
 await job;
 assert.match(p.texts(articles()[0]).join('|'),/该会话的额度已单独保存。/);
 assert.match(p.texts(articles()[0]).join('|'),/额度：单独设置/);
 assert.equal(p.inputs(articles()[0])[0].value,'9');
 assert.equal(p.button(articles()[0],'恢复账号默认').disabled,false);
 assert.equal(p.texts(articles()[1]).join('|').includes('额度已单独保存'),false);

 const reset=p.button(articles()[0],'恢复账号默认').onclick();
 const restore=p.requests.at(-1);
 assert.equal(restore.operation,'reply_quota.scope_reset');
 assert.deepEqual(JSON.parse(JSON.stringify(restore.arguments)),
  {scope_id:'reply-scope-v1-private',account:'10001',expected:'private-v2'});
 await p.reply(restore,privateRow({changed:true,inherited:true,version:'private-v3'}));
 await reset;
 assert.match(p.texts(articles()[0]).join('|'),/已恢复为继承账号默认额度。/);
 assert.equal(p.inputs(articles()[0])[0].value,'200');
 assert.equal(p.button(articles()[0],'恢复账号默认').disabled,true);
});

test('saving an inheriting conversation with its current numbers makes it independent',async()=>{
 const p=await unlocked();
 const articles=()=>p.tags(p.node('reply-quota-accounts'),'article');
 const open=p.button(p.node('reply-quota-accounts').children[0],'查看已登记会话').onclick();
 await p.reply(p.requests.at(-1),conversations([privateRow()]));
 await open;
 assert.match(p.texts(articles()[0]).join('|'),/额度：继承账号默认/);
 assert.equal(p.button(articles()[0],'恢复账号默认').disabled,true);
 // The page submits the whole form instead of deciding that nothing changed.
 const job=p.button(articles()[0],'保存更改').onclick();
 const request=p.requests.at(-1);
 assert.equal(request.operation,'reply_quota.scope_save');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {scope_id:'reply-scope-v1-private',account:'10001',expected:'private-v1',
   changes:{private_daily_replies:200}});
 await p.reply(request,privateRow({changed:true,inherited:false,version:'private-v2'}));
 await job;
 assert.match(p.texts(articles()[0]).join('|'),/该会话的额度已单独保存。/);
 assert.match(p.texts(articles()[0]).join('|'),/额度：单独设置/);
 assert.equal(p.button(articles()[0],'恢复账号默认').disabled,false);
});

test('a version conflict asks for a reload and never overwrites silently',async()=>{
 const p=await unlocked();
 const block=()=>p.node('reply-quota-accounts').children[0];
 p.inputs(block())[0].value='7';
 p.inputs(block())[0].oninput();
 const job=p.button(block(),'保存更改').onclick();
 const request=p.requests.at(-1);
 assert.equal(request.arguments.expected,null);
 const count=p.requests.length;
 await p.fail(request,'edit_conflict','账号默认额度已改变，请重新读取后再保存。');
 await job;
 assert.match(p.texts(block()).join('|'),/额度已在别处改变，请重新读取后再保存。/);
 assert.equal(p.button(block(),'保存更改').disabled,true);
 // A conflicted page also stops editing instead of accepting text it cannot save.
 assert.equal(p.inputs(block())[0].disabled,true);
 assert.equal(p.requests.length,count);
 // Unsaved text is kept so the user sees what was rejected.
 assert.equal(p.inputs(block())[0].value,'7');
 p.node('reply-quota-refresh').onclick();
 assert.equal(p.requests.at(-1).operation,'reply_quota.summary');
 await p.reply(p.requests.at(-1),summary([account({version:'account-v9',
  settings:{...DEFAULTS,private_daily_replies:33}})]));
 assert.equal(p.button(block(),'保存更改').disabled,false);
 assert.equal(p.inputs(block())[0].disabled,false);
 assert.deepEqual(p.inputs(block()).map(node=>node.value),['33','5','20','10']);
 p.button(block(),'保存更改').onclick();
 assert.equal(p.requests.at(-1).arguments.expected,'account-v9');
});

test('conversations are paged forward and back with the cursor the backend returned',async()=>{
 const p=await unlocked();
 const block=()=>p.node('reply-quota-accounts').children[0];
 const open=p.button(block(),'查看已登记会话').onclick();
 await p.reply(p.requests.at(-1),conversations([privateRow(),groupRow()],
  {total:25,next:'reply-scope-v1-20',previous:null}));
 await open;
 assert.equal(p.button(block(),'上一页').disabled,true);
 assert.equal(p.button(block(),'下一页').disabled,false);
 const forward=p.button(block(),'下一页').onclick();
 assert.deepEqual(JSON.parse(JSON.stringify(p.requests.at(-1).arguments)),
  {platform:'qq',account:'10001',after:'reply-scope-v1-20'});
 await p.reply(p.requests.at(-1),conversations([privateRow({scope_id:'reply-scope-v1-last'})],
  {total:25,offset:20,next:null,previous:''}));
 await forward;
 assert.match(p.texts(block()).join('|'),/本页显示第 21 至 21 个/);
 assert.equal(p.button(block(),'下一页').disabled,true);
 assert.equal(p.button(block(),'上一页').disabled,false);
 const backward=p.button(block(),'上一页').onclick();
 assert.deepEqual(JSON.parse(JSON.stringify(p.requests.at(-1).arguments)),
  {platform:'qq',account:'10001',after:''});
 await p.reply(p.requests.at(-1),conversations([privateRow(),groupRow()],
  {total:25,next:'reply-scope-v1-20',previous:null}));
 await backward;
 assert.equal(p.tags(p.node('reply-quota-accounts'),'article').length,2);
});

test('later account pages are appended without losing edits or repeating accounts',async()=>{
 const first=account();
 const p=await unlocked(summary([first],{total:3,next:'qq:30001'}));
 const more=p.button(p.node('reply-quota-accounts'),
  '继续读取更多账号（已显示 1/3 个）').onclick();
 assert.deepEqual(JSON.parse(JSON.stringify(p.requests.at(-1).arguments)),{after:'qq:30001'});
 const second=account({key:'qq:30002',account:'30002',label:null,selected:false,version:null});
 await p.reply(p.requests.at(-1),summary([second],{total:3,offset:1,next:'qq:30003'}));
 await more;
 assert.match(p.node('reply-quota-status').textContent,/已读取 2\/3 个账号/);
 const blocks=()=>p.node('reply-quota-accounts').children.filter(node=>node.className==='reply-quota-account');
 assert.equal(blocks().length,2);
 // An edit typed on an earlier page survives loading the next one.
 p.inputs(blocks()[0])[0].value='77';
 p.inputs(blocks()[0])[0].oninput();
 const last=p.button(p.node('reply-quota-accounts'),
  '继续读取更多账号（已显示 2/3 个）').onclick();
 assert.deepEqual(JSON.parse(JSON.stringify(p.requests.at(-1).arguments)),{after:'qq:30003'});
 await p.reply(p.requests.at(-1),summary([account({key:'qq:30003',account:'30003',label:null,
  selected:false})],{total:3,offset:2,next:null}));
 await last;
 assert.equal(blocks().length,3);
 assert.deepEqual(blocks().map(node=>node.children[0].textContent),
  ['QQ · 10001（合成账号） · 当前使用','QQ · 30002 · 已保存','QQ · 30003 · 已保存']);
 assert.equal(p.inputs(blocks()[0])[0].value,'77');
 // No further page is offered once the backend reports the last one.
 assert.equal(p.buttons(p.node('reply-quota-accounts'))
  .some(node=>node.textContent.startsWith('继续读取更多账号')),false);
 // The last page's account is editable immediately.
 p.inputs(blocks()[2])[0].value='31';
 const saveJob=p.button(blocks()[2],'保存更改').onclick();
 const request=p.requests.at(-1);
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {platform:'qq',account:'30003',expected:null,
   changes:{private_daily_replies:31,group_session_replies:5,group_session_decisions:20,
    group_session_minutes:10}});
 await p.reply(request,{platform:'qq',account:'30003',version:'v-30003',changed:true,
  settings:{...DEFAULTS,private_daily_replies:31}});
 await saveJob;
 assert.match(p.texts(blocks()[2]).join('|'),/账号默认额度已保存/);
});

test('an allow-all policy and a running platform are described without claiming a revocation',async()=>{
 const p=await unlocked(summary([account({platform_running:true,manual:0,eligible:0,
  allow_all:{private:true,group:false}})]));
 const block=p.node('reply-quota-accounts').children[0];
 const header=p.texts(block).join('|');
 assert.match(header,/QQ平台当前正在运行，保存后的额度在下一次处理时生效。/);
 assert.match(header,/已登记会话 2 个，当前有效 0 个，手动勾选 0 个。/);
 assert.match(header,/允许所有好友已开启；运行时按关系核验后生效，不是撤销。/);
 assert.equal(header.includes('已撤销'),false);
 assert.equal(header.includes('该账号'),false);

 const open=p.button(block,'查看已登记会话').onclick();
 await p.reply(p.requests.at(-1),conversations([privateRow({authorized:false,manual:false,
  allow_all:true,status:'允许所有好友已开启；平台运行并按关系核验后生效，这不是撤销'})]));
 await open;
 const text=p.texts(p.node('reply-quota-accounts')).join('|');
 assert.match(text,/授权状态：允许所有好友已开启；平台运行并按关系核验后生效，这不是撤销/);
 assert.equal(text.includes('已撤销'),false);
});

test('an empty conversation page shows no inverted range',async()=>{
 const p=await unlocked(summary([account({registered:0,manual:0,eligible:0})]));
 const open=p.button(p.node('reply-quota-accounts').children[0],'查看已登记会话').onclick();
 await p.reply(p.requests.at(-1),conversations([],{total:0}));
 await open;
 const text=p.texts(p.node('reply-quota-accounts')).join('|');
 assert.match(text,/单会话额度：已登记会话共 0 个。/);
 assert.match(text,/这个账号还没有已登记的会话。账号默认额度仍可修改。/);
 assert.equal(text.includes('第 1 至 0'),false);
});

test('saved labels are rendered as text and never parsed as markup',async()=>{
 const p=page();
 p.state(true,true);
 await p.reply(p.requests.at(-1),summary([account({label:'<img src=x onerror=alert(1)>'})]));
 const block=p.node('reply-quota-accounts').children[0];
 assert.equal(p.tags(block,'img').length,0);
 assert.match(p.texts(block).join('|'),/（<img src=x onerror=alert\(1\)>）/);
 const open=p.button(block,'查看已登记会话').onclick();
 await p.reply(p.requests.at(-1),conversations([
  privateRow({name:'<b>合成</b>',label:'私聊 · <b>合成</b>'})]));
 await open;
 const article=p.tags(p.node('reply-quota-accounts'),'article')[0];
 assert.equal(p.tags(article,'b').length,0);
 assert.equal(p.tags(article,'h4')[0].textContent,'私聊 · <b>合成</b>');
});

test('an empty account list explains that only verified registrations can be edited',async()=>{
 const p=page();
 p.state(true,true);
 await p.reply(p.requests.at(-1),summary([],{total:0}));
 const text=p.texts(p.node('reply-quota-accounts')).join('|');
 assert.match(text,/本机还没有可编辑额度的账号/);
 assert.match(text,/已保存并曾核验/);
 // The rules for both limits stay readable even with nothing to edit.
 assert.match(p.node('reply-quota-rule').textContent,/自然日统计/);
 assert.match(p.node('reply-quota-rule').textContent,/这不是计费额度/);
});
