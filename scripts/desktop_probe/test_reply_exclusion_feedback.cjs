// Reply exclusion page tests use only synthetic replies; nothing here reaches a platform.
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
  settings:{...DEFAULTS},registered:2,manual:2,eligible:2,excluded:0,
  allow_all:{private:false,group:false},platform_running:false,...overrides};
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
  excluded:false,exclusion_version:null,excluded_since:null,reply_from:null,
  status:'当前有效：已授权回复该会话',account:'10001',platform:'qq',
  settings:{private_daily_replies:200},inherited:true,version:'private-v1',
  fields:['private_daily_replies'],limit:200,
  usage:{confirmed:2,reserved:1,uncertain:1},used:4,...overrides};
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

async function oneConversation(row,overrides){
 const p=await unlocked();
 const open=p.button(p.node('reply-quota-accounts').children[0],'查看已登记会话').onclick();
 await p.reply(p.requests.at(-1),conversations([row],overrides));
 await open;
 return {p,articles:()=>p.tags(p.node('reply-quota-accounts'),'article')};
}

test('a conversation card offers a stop button that is neither a quota save nor an account pause',async()=>{
 const {p,articles}=await oneConversation(privateRow());
 const text=p.texts(articles()[0]).join('|');
 assert.match(text,/排除状态：未排除。/);
 assert.match(text,/授权状态：当前有效：已授权回复该会话/);
 assert.equal(p.button(articles()[0],'解除排除').disabled,true);
 assert.equal(p.button(articles()[0],'停用此会话').disabled,false);
 assert.match(p.texts(p.node('reply-quota-accounts')).join('|'),/停用此会话”只停该会话/);
});

test('stopping one conversation keeps an unsaved quota draft and sends only the exclusion',async()=>{
 const {p,articles}=await oneConversation(privateRow());
 p.inputs(articles()[0])[0].value='9';
 p.inputs(articles()[0])[0].oninput();
 const job=p.button(articles()[0],'停用此会话').onclick();
 const request=p.requests.at(-1);
 assert.equal(request.operation,'reply_exclusion.exclude');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {scope_id:'reply-scope-v1-private',account:'10001',expected:null});
 await p.flush(2);
 // While the request is in flight nothing else can be written from this card.
 assert.equal(p.button(articles()[0],'停用此会话').disabled,true);
 assert.equal(p.button(articles()[0],'解除排除').disabled,true);
 assert.equal(p.button(articles()[0],'保存更改').disabled,true);
 await p.reply(request,privateRow({changed:true,excluded:true,exclusion_version:'ex-1',
  excluded_since:1792922400,authorized:false,
  status:'已排除：已停用该好友，不会回复；解除后只回应解除之后的新消息',
  message:'已停用此私聊：该会话不会回复，其他会话、账号与平台保持原状；解除后只回应解除之后的新消息。'}));
 await job;
 const live=articles()[0];
 assert.match(p.texts(live).join('|'),/排除状态：已排除（停用于/);
 assert.match(p.texts(live).join('|'),/授权状态：已排除：已停用该好友/);
 assert.match(p.texts(live).join('|'),/已停用此私聊：该会话不会回复/);
 assert.equal(p.button(live,'停用此会话').disabled,true);
 assert.equal(p.button(live,'解除排除').disabled,false);
 // The typed quota value is still a draft, not something the exclusion saved.
 assert.equal(p.inputs(live)[0].value,'9');
 assert.equal(p.inputs(live)[0].disabled,false);
 assert.equal(p.requests.some(item=>item.operation==='reply_quota.scope_save'),false);
});

test('lifting sends the exclusion version and reports what still blocks a reply',async()=>{
 const {p,articles}=await oneConversation(privateRow({excluded:true,exclusion_version:'ex-1',
  excluded_since:1792922400,authorized:false,
  status:'已排除：已停用该好友，不会回复；解除后只回应解除之后的新消息'}));
 p.inputs(articles()[0])[0].value='4';
 p.inputs(articles()[0])[0].oninput();
 const job=p.button(articles()[0],'解除排除').onclick();
 const request=p.requests.at(-1);
 assert.equal(request.operation,'reply_exclusion.restore');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {scope_id:'reply-scope-v1-private',account:'10001',expected:'ex-1'});
 await p.reply(request,privateRow({changed:true,excluded:false,exclusion_version:'ex-2',
  reply_from:1792922460,authorized:false,manual:false,allow_all:true,
  status:'允许所有好友已开启；平台运行并按关系核验后生效，这不是撤销',
  message:'已解除排除：允许所有好友已开启，但要平台运行并按关系核验后才可能回复；解除排除不等于已恢复回复。'}));
 await job;
 const live=articles()[0];
 const text=p.texts(live).join('|');
 assert.match(text,/排除状态：已于 .* 解除；只回应这之后的新消息/);
 assert.match(text,/已解除排除：允许所有好友已开启/);
 assert.match(text,/解除排除不等于已恢复回复/);
 assert.equal(text.includes('回复已恢复'),false);
 assert.equal(p.inputs(live)[0].value,'4');
 assert.equal(p.button(live,'停用此会话').disabled,false);
 assert.equal(p.button(live,'解除排除').disabled,true);
 assert.equal(p.requests.some(item=>item.operation==='reply_quota.scope_save'),false);
});

test('a refused stop stays visible next to that conversation only',async()=>{
 const p=await unlocked();
 const open=p.button(p.node('reply-quota-accounts').children[0],'查看已登记会话').onclick();
 await p.reply(p.requests.at(-1),conversations([privateRow()]));
 await open;
 const articles=()=>p.tags(p.node('reply-quota-accounts'),'article');
 const job=p.button(articles()[0],'停用此会话').onclick();
 await p.fail(p.requests.at(-1),'reply_quota_unregistered',
  '该会话尚未登记或所属账号未完成核验，不能修改额度。');
 await job;
 assert.match(p.texts(articles()[0]).join('|'),/该会话尚未登记或所属账号未完成核验/);
 assert.equal(p.button(articles()[0],'停用此会话').disabled,false);
 assert.equal(p.inputs(articles()[0])[0].disabled,false);
});

test('an exclusion version conflict locks the card until the page is re-read',async()=>{
 const {p,articles}=await oneConversation(privateRow());
 p.inputs(articles()[0])[0].value='6';
 p.inputs(articles()[0])[0].oninput();
 const job=p.button(articles()[0],'停用此会话').onclick();
 await p.fail(p.requests.at(-1),'edit_conflict','单会话排除记录无效或已改变，请重新读取');
 await job;
 const live=articles()[0];
 assert.match(p.texts(live).join('|'),/已在别处改变，请重新读取后再保存。/);
 assert.equal(p.button(live,'停用此会话').disabled,true);
 assert.equal(p.button(live,'解除排除').disabled,true);
 assert.equal(p.inputs(live)[0].disabled,true);
 assert.equal(p.inputs(live)[0].value,'6');
 const count=p.requests.length;
 p.button(live,'停用此会话').onclick();
 await p.flush(2);
 assert.equal(p.requests.length,count);
 p.node('reply-quota-refresh').onclick();
 assert.equal(p.requests.at(-1).operation,'reply_quota.summary');
 await p.reply(p.requests.at(-1),summary());
 assert.equal(p.button(p.node('reply-quota-accounts').children[0],'查看已登记会话').disabled,false);
});

test('the account counts follow the returned summary without losing drafts or pages',async()=>{
 const first=account();
 const second=account({key:'qq:20001',account:'20001',label:null,selected:false});
 const p=await unlocked(summary([first,second],{total:2}));
 const blocks=()=>p.node('reply-quota-accounts').children.filter(node=>node.className==='reply-quota-account');
 const header=()=>p.texts(blocks()[0]).join('|');
 assert.match(header(),/已登记会话 2 个，当前有效 2 个，手动勾选 2 个。/);
 assert.equal(header().includes('已排除'),false);
 // Both accounts keep an expanded conversation page and an unsaved quota draft.
 const rows=[[privateRow()],[privateRow({scope_id:'reply-scope-v1-private-2',account:'20001'})]];
 let index=0;
 for(const block of blocks()){
  const open=p.button(block,'查看已登记会话').onclick();
  await p.reply(p.requests.at(-1),conversations(rows[index++]));
  await open;
 }
 const articles=()=>p.tags(p.node('reply-quota-accounts'),'article');
 assert.equal(articles().length,2);
 p.inputs(articles()[0])[0].value='8';
 p.inputs(articles()[0])[0].oninput();
 p.inputs(articles()[1])[0].value='5';
 p.inputs(articles()[1])[0].oninput();

 const job=p.button(blocks()[0],'停用此会话').onclick();
 const request=p.requests.at(-1);
 assert.equal(request.operation,'reply_exclusion.exclude');
 await p.reply(request,privateRow({changed:true,excluded:true,exclusion_version:'ex-1',
  excluded_since:1792922400,authorized:false,
  status:'已排除：已停用该好友，不会回复；解除后只回应解除之后的新消息',
  counts:{registered:2,manual:2,eligible:1,excluded:1},
  message:'已停用此私聊：该会话不会回复。'}));
 await job;
 assert.match(header(),/已登记会话 2 个，当前有效 1 个，手动勾选 2 个，已排除 1 个。/);
 // The other account, both expanded pages and both drafts are untouched.
 assert.equal(articles().length,2);
 assert.equal(p.inputs(articles()[0])[0].value,'8');
 assert.equal(p.inputs(articles()[1])[0].value,'5');
 assert.equal(p.texts(blocks()[1]).join('|').includes('已排除 1 个'),false);
 assert.match(p.texts(blocks()[1]).join('|'),/当前有效 2 个/);

 const lifted=p.button(articles()[0],'解除排除').onclick();
 const restore=p.requests.at(-1);
 assert.equal(restore.operation,'reply_exclusion.restore');
 await p.reply(restore,privateRow({changed:true,excluded:false,exclusion_version:'ex-2',
  reply_from:1792922460,authorized:true,
  counts:{registered:2,manual:2,eligible:2,excluded:0},
  message:'已解除排除：按原有授权继续，只会回应解除之后的新消息；排除期间的消息不会补回复。'}));
 await lifted;
 assert.match(header(),/当前有效 2 个，手动勾选 2 个。/);
 assert.equal(header().includes('已排除'),false);
 assert.equal(p.inputs(articles()[0])[0].value,'8');
 assert.equal(p.inputs(articles()[1])[0].value,'5');
});

test('a late exclusion reply cannot repaint a locked page',async()=>{
 const {p,articles}=await oneConversation(privateRow());
 const job=p.button(articles()[0],'停用此会话').onclick();
 const request=p.requests.at(-1);
 p.state(true,false);
 await p.reply(request,privateRow({excluded:true,exclusion_version:'ex-1',excluded_since:1}));
 await job;
 assert.equal(p.node('reply-quota-accounts').children.length,0);
 assert.equal(p.node('reply-quota-status').textContent,'');
 assert.equal(p.requests.filter(item=>item.operation.startsWith('reply_exclusion.')).length,1);
});
