// Record sharing page tests. Every value here is synthetic: nothing reaches a
// database, a platform, a model or the real desktop shell.
//
// The page runs on a tiny fake DOM plus a fake WebView host, so a test can hold one
// reply back, deliver replies out of order, fire the request timeout and click the
// same button twice. Assertions are made on what the page *shows* and on the exact
// operations it posts, never on the page source text and never on a copy of the
// page's own helper functions.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');

function page(){
 const nodes=new Map(),events={},requests=[],listeners=[],timers=new Map(),markup=[];
 const element=tag=>{
  let own='';
  const node={tagName:tag,hidden:false,disabled:false,value:'',className:'',
   type:'',min:'',max:'',step:'',checked:false,attributes:{},children:[],
   append(...kids){for(const kid of kids)this.children.push(kid);},
   replaceChildren(...kids){this.children=kids;this.value='';},
   setAttribute(name,value){this.attributes[name]=value;},
   removeAttribute(name){delete this.attributes[name];}};
  // Like the real DOM, the text of an element includes the text of its children, so a
  // label built from a checkbox plus a text node can be read back in a test.
  Object.defineProperty(node,'textContent',{
   get(){
    let joined=own;
    for(const kid of node.children)joined+=kid&&kid.textContent?kid.textContent:'';
    return joined;
   },
   set(value){own=String(value);}});
  // Assigning raw markup would bypass escaping; record it so a test can refuse it.
  for(const name of ['innerHTML','outerHTML','insertAdjacentHTML']){
   Object.defineProperty(node,name,{set(value){markup.push({tag,name,value});},get(){return '';}});
  }
  return node;
 };
 const node=id=>{if(!nodes.has(id))nodes.set(id,element('div'));return nodes.get(id);};
 let uuid=0,timer=0;
 const webview={addEventListener(_,fn){listeners.push(fn);},postMessage(request){requests.push(request);}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/sharing.js'),'utf8'),{
  document:{getElementById:node,createElement:element,
   createTextNode(text){return {tagName:'#text',textContent:String(text),children:[]};}},
  window:{chrome:{webview},addEventListener(name,fn){events[name]=fn;}},
  crypto:{randomUUID:()=>String(++uuid)},
  setTimeout(fn){timers.set(++timer,fn);return timer;},
  clearTimeout(id){timers.delete(id);},
  Date,Number,Error,Map,Set,Array,Object,String,JSON,Promise,Boolean,Math,RegExp});
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
 function walk(target,visit){
  for(const kid of target.children||[]){visit(kid);walk(kid,visit);}
 }
 function texts(target){const out=[];walk(target,n=>{if(n.textContent)out.push(n.textContent);});return out;}
 function text(target){return texts(target).join('|');}
 function tags(target,name){const found=[];walk(target,n=>{if(n.tagName===name)found.push(n);});return found;}
 function buttons(target,label){
  const found=tags(target,'button');
  return label===undefined?found:found.filter(node=>node.textContent.includes(label));
 }
 function inputs(target,type){
  return tags(target,'input').filter(node=>type===undefined||node.type===type);
 }
 function selects(target){return tags(target,'select');}
 const latest=()=>requests.at(-1);
 const count=operation=>requests.filter(item=>item.operation===operation).length;
 return {node,requests,markup,state,reply,fail,settle,flush,fireTimers,texts,text,tags,buttons,
  inputs,selects,latest,count};
}

// --- synthetic backend payloads --------------------------------------------
function item(overrides={}){
 return {kind:'memories',identifier:'m-ok',label:'记忆 · m-ok',cursor:'memories/m-ok',version:'v1',
  classification:'personal',classification_label:'仅归属账号私聊',owners:['qq:10001'],
  audience:'private',valid:true,expires_at:null,source_count:1,policy_version:'p1',usable:true,
  fact:false,foundation:false,manageable:true,grants:0,grants_limited:false,...overrides};
}
function listing(records,overrides={}){
 return {records,count:records.length,limit:20,offset:'',next:null,page_max:50,
  kinds:['diaries','knowledge','memories'],kind_labels:{diaries:'日记',knowledge:'知识',memories:'记忆'},
  meaning:'这里只调整一条资料可以被谁使用。',...overrides};
}
function target(key,label){
 return {key,platform:key.split(':')[0],user_id:key.split(':')[1],scope:null,
  label:label||'QQ · '+key};
}
function targetsPage(records,next){
 return {targets:records,next:next||null,meaning:'这里只列出已经登记过的账号。'};
}
function policyView(overrides={}){
 return {classification:'personal',classification_label:'仅归属账号私聊',owners:['qq:10001'],
  conversation:null,audience:'private',valid:true,expires_at:null,source_count:1,
  sources:['memories/m-src'],source_versions:[['memories/m-src','v1']],sources_limited:false,
  pins_complete:true,version:'p1',...overrides};
}
function grantRow(overrides={}){
 return {id:'g1',grantee:'qq:20002',owner:'qq:10001',purposes:['reply'],purpose_labels:['回复'],
  basis:'owner_admin',expires_at:1800000000,active:true,revoked:false,epoch_current:true,
  target:'QQ · 20002',...overrides};
}
// ``count`` grants with distinct ids and labels, starting at ``start``.
function grantList(count,start){
 const first=start||1;
 return Array.from({length:count},(_,index)=>{
  const number=first+index;
  return grantRow({id:'g'+number,grantee:'qq:'+(20000+number),target:'QQ · '+(20000+number)});
 });
}
function report(overrides={}){
 return {ok:true,nodes:2,issues:[],total_issues:0,limited:false,...overrides};
}
function impact(overrides={}){
 return {fingerprint:'fp',affected_count:1,sample:['memories/m-ok'],sample_limited:false,...overrides};
}
// One concrete request shape as the backend reports it: shape name, its label, the
// purpose and whether AccessService really allowed that read.
function effect(overrides={}){
 return {shape:'private_reply',label:'归属账号私聊',audience:'private',audience_label:'私聊',
  purpose:'reply',purpose_label:'回复',allowed:true,...overrides};
}
function scopeEntry(overrides={}){
 return {resource:'memories/m-ok',present:true,policy:policyView(),other_grants:0,
  remaining_basis:'none',...overrides};
}
function scopeOf(entries,overrides={}){
 return {resources:entries,count:entries.length,
  state_labels:{public:'分类公开，撤销后仍可读',owner:'该账号是归属账号，撤销后仍可读',
   other_grant:'仍有一条有效授权可读',conversation:'仅原会话按分类可读',
   blocked:'资料停用/过期/隔离，本来就不能读',missing:'权限记录缺失',
   none:'撤销后这个账号不可读'},...overrides};
}
function record(overrides={}){
 return {kind:'memories',identifier:'m-ok',resource:'memories/m-ok',record_version:'v1',locked:false,
  policy:policyView(),owner_options:['qq:10001'],grants:[],grants_limited:false,
  grants_next:null,grants_active:0,grants_total:0,grants_revoked:0,grants_stale_epoch:0,
  source:report(),blockers:[],impact:impact(),
  classifications:[{value:'personal',label:'仅归属账号私聊',rank:3},
   {value:'restricted',label:'仅显式授权（默认无人可读）',rank:2}],
  purposes:[{value:'reply',label:'回复'},{value:'maintenance',label:'维护'}],
  days:{default:30,max:365},meaning:'这里只调整一条资料可以被谁使用，不是允许回复该联系人。',
  ...overrides};
}
function grantPreview(overrides={}){
 return {token:'tok-grant',action:'grant',resource:'memories/m-ok',kind:'memories',identifier:'m-ok',
  before:{grants:0,grants_limited:false},
  after:{grants:1,grant:{grantee:'qq:20002',owner:'qq:10001',purposes:['reply'],
   purpose_labels:['回复'],days:30,expires_at:1800000000,target:'QQ · 20002'}},
  source:report(),blockers:[],impact:impact(),
  effect:[effect()],effect_before:[effect({allowed:false})],configured_purposes:['reply'],
  will_apply:true,written:false,
  notice:'确认后只新增一条私聊授权：该账号在私聊中使用所选用途时可以读取这条资料；'+
   '不会授权任何会话、不启动平台、不改变回复额度。',...overrides};
}
function classifyPreview(overrides={}){
 return {token:'tok-classify',action:'classify',resource:'memories/m-ok',kind:'memories',
  identifier:'m-ok',direction:'收窄',before:policyView(),
  after:policyView({classification:'restricted',classification_label:'仅显式授权（默认无人可读）',
   version:'p2'}),
  source:report(),blockers:[],impact:impact(),
  effect:[effect({allowed:false})],effect_before:[effect({allowed:true})],
  configured_purposes:['reply','maintenance'],
  will_apply:false,written:false,owner_checked:'QQ · 10001',
  notice:'确认后只改变这条资料的分类；归属账号、真实来源与版本、有效状态和到期时间保持原样。',
  ...overrides};
}
function revokePreview(overrides={}){
 const resources=Array.isArray(overrides.resources)?overrides.resources:['memories/m-ok'];
 const base={token:'tok-revoke',action:'revoke',resource:'memories/m-ok',kind:'memories',
  identifier:'m-ok',before:{grants:1,grant:grantRow()},after:{grants:0},
  scope:scopeOf(resources.map(resource=>scopeEntry({resource}))),scope_count:resources.length,
  scope_dependents:[],scope_dependents_count:0,scope_dependents_limited:false,
  source:report(),blockers:[],impact:impact(),
  effect:[effect({allowed:false})],effect_before:[effect({allowed:true})],
  remaining_readable:[],will_apply:true,written:false,
  notice:'确认后只撤销这一条授权，并同时影响上面列出的全部资料；其他授权、分类、来源和回复授权保持原样。'};
 const merged={...base,...overrides};
 if(overrides.scope_count===undefined){
  const count=merged.scope&&typeof merged.scope.count==='number'?merged.scope.count:resources.length;
  merged.scope_count=count;
 }
 return merged;
}
function numbered(prefix,count){
 return Array.from({length:count},(_,index)=>'memories/'+prefix+'-'+String(index).padStart(2,'0'));
}

// --- harness steps ---------------------------------------------------------
function detailBox(p){return p.node('sharing-detail');}
function previewBox(p){return p.node('sharing-preview');}
function fields(p){return p.selects(detailBox(p));}

async function ready(records){
 const p=page();
 p.state(true,true);
 const request=p.latest();
 assert.equal(request.operation,'sharing.records');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),{after:'',limit:20});
 await p.reply(request,listing(records===undefined?[item()]:records));
 await p.flush(2);
 return p;
}
// Select a listed record and answer its detail read (and the account read it starts).
async function openRow(p,index,detail,accounts){
 const rows=p.buttons(p.node('sharing-items'));
 const job=rows[index===undefined?0:index].onclick();
 const request=p.latest();
 assert.equal(request.operation,'sharing.record');
 await p.reply(request,detail||record());
 const pending=p.latest();
 if(pending!==request&&pending.operation==='sharing.targets')
  await p.reply(pending,accounts||targetsPage([target('qq:20002')]));
 await job;
 await p.flush(3);
 return p;
}

test('unlocking reads the manageable list and locking clears it again',async()=>{
 const p=page();
 // A locked page posts nothing, even when the button is clicked.
 p.state(true,false);
 assert.equal(p.requests.length,0);
 p.node('sharing-load').onclick();
 await p.flush(2);
 assert.equal(p.requests.length,0);
 assert.equal(p.text(p.node('sharing-items')),'');
 // Unlocking reads page one from scratch.
 p.state(true,true);
 assert.equal(p.latest().operation,'sharing.records');
 await p.reply(p.latest(),listing([item()]));
 await p.flush(2);
 assert.match(p.text(p.node('sharing-items')),/记忆 · m-ok · 仅归属账号私聊 · 授权 0 个/);
 assert.equal(p.node('sharing-status').textContent,'已读取 1 条可管理的资料。');
 assert.match(p.text(p.node('sharing-items')),/不显示正文/);
 assert.equal(p.node('sharing-load').disabled,false);
 // The area follows the management panel, including when nothing else changed.
 p.node('management-panel').hidden=true;
 p.state(true,true);
 assert.equal(p.node('sharing-panel').hidden,true);
 p.node('management-panel').hidden=false;
 p.state(true,true);
 assert.equal(p.node('sharing-panel').hidden,false);
 // Locking drops the list and everything derived from it.
 p.state(true,false);
 assert.equal(p.text(p.node('sharing-items')),'');
 assert.equal(p.node('sharing-status').textContent,'');
 assert.equal(p.text(detailBox(p)),'');
 assert.equal(p.node('sharing-preview-area').hidden,true);
 assert.equal(p.node('sharing-load').disabled,true);
});

test('a late list reply from before a lock cannot revive the page',async()=>{
 const p=await ready();
 p.node('sharing-load').onclick();
 const late=p.latest();
 assert.equal(late.operation,'sharing.records');
 // The lock arrives while that read is still on its way.
 p.state(true,false);
 await p.flush(2);
 assert.equal(p.text(p.node('sharing-items')),'');
 assert.equal(p.node('sharing-status').textContent,'');
 // The old reply arrives afterwards and must not paint any state back.
 await p.reply(late,listing([item({identifier:'m-old',label:'记忆 · m-old'})]));
 await p.flush(3);
 assert.equal(p.text(p.node('sharing-items')),'');
 assert.equal(p.node('sharing-status').textContent,'');
 assert.equal(p.node('sharing-load').disabled,true);
 // Unlocking reads again; the dead reply can never supersede the fresh result.
 p.state(true,true);
 const fresh=p.latest();
 assert.equal(fresh.operation,'sharing.records');
 await p.reply(fresh,listing([item({identifier:'m-new',label:'记忆 · m-new'})]));
 await p.flush(2);
 assert.match(p.text(p.node('sharing-items')),/记忆 · m-new/);
 assert.equal(p.text(p.node('sharing-items')).includes('记忆 · m-old'),false);
 assert.equal(p.node('sharing-status').textContent,'已读取 1 条可管理的资料。');
});

test('a failed list read leaves no list, detail or preview behind',async()=>{
 const p=await openRow(await ready(),0,record({grants:[grantRow()]}));
 assert.match(p.text(detailBox(p)),/已有私聊授权/);
 // A revoke preview is on screen.
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 await p.reply(p.latest(),revokePreview());
 await p.flush(2);
 assert.equal(p.node('sharing-preview-area').hidden,false);
 assert.match(p.text(previewBox(p)),/确认撤销授权/);
 // The refresh fails: nothing editable may survive it.
 p.node('sharing-load').onclick();
 await p.fail(p.latest(),'sharing_query','每次只能查看 1 至 50 条资料。');
 await p.flush(3);
 assert.match(p.node('sharing-status').textContent,/每次只能查看 1 至 50 条资料/);
 assert.match(p.node('sharing-status').textContent,/重试/);
 assert.equal(p.text(p.node('sharing-items')),'还没有读取可管理的资料。点“读取可管理的资料”开始。');
 assert.equal(p.text(detailBox(p)),'先在上方选择一条资料，这里会显示它当前的分类、归属、来源和已有授权。');
 assert.equal(p.node('sharing-preview-area').hidden,true);
 // The page is idle again: the same button retries and a fresh record starts clean.
 p.node('sharing-load').onclick();
 await p.reply(p.latest(),listing([item()]));
 await p.flush(2);
 await openRow(p,0,record());
 assert.equal(p.inputs(detailBox(p),'number')[0].value,'30');
 assert.equal(p.count('sharing.commit'),0);
});

test('a refreshed list that drops the selected record invalidates its late detail reply',async()=>{
 const p=await ready([item(),item({identifier:'m-two',label:'记忆 · m-two'})]);
 // Select the first row; its detail read stays pending.
 const rows=p.buttons(p.node('sharing-items'));
 const job=rows[0].onclick();
 const detailRequest=p.latest();
 assert.equal(detailRequest.operation,'sharing.record');
 // The list is read again and no longer contains that record.
 p.node('sharing-load').onclick();
 await p.reply(p.latest(),listing([item({identifier:'m-two',label:'记忆 · m-two'})]));
 await p.flush(3);
 assert.match(p.text(detailBox(p)),/刚才选中的资料已不在这次读取的结果里/);
 assert.equal(p.text(p.node('sharing-items')).includes('记忆 · m-ok'),false);
 // The old detail reply arrives last and must not repaint the dropped record.
 await p.reply(detailRequest,record());
 await job;
 await p.flush(2);
 assert.equal(p.text(detailBox(p)).includes('资料版本'),false);
 assert.match(p.text(detailBox(p)),/刚才选中的资料/);
 assert.equal(p.node('sharing-preview-area').hidden,true);
});

test('a refreshed list that keeps the selected record keeps its pending detail reply',async()=>{
 const p=await ready([item()]);
 const job=p.buttons(p.node('sharing-items'))[0].onclick();
 const detailRequest=p.latest();
 p.node('sharing-load').onclick();
 await p.reply(p.latest(),listing([item()]));
 await p.flush(3);
 assert.match(p.text(detailBox(p)),/正在读取这条资料的共享设置/);
 await p.reply(detailRequest,record());
 const accounts=p.latest();
 if(accounts!==detailRequest)await p.reply(accounts,targetsPage([target('qq:20002')]));
 await job;
 await p.flush(3);
 assert.match(p.text(detailBox(p)),/资料版本：v1/);
 assert.equal(p.text(detailBox(p)).includes('正在读取这条资料'),false);
});

test('a late detail reply cannot replace a newer selection',async()=>{
 const p=await ready([item(),item({identifier:'m-two',label:'记忆 · m-two'})]);
 const rows=p.buttons(p.node('sharing-items'));
 const older=rows[0].onclick();
 const first=p.latest();
 const newer=rows[1].onclick();
 const second=p.latest();
 assert.notEqual(first.request_id,second.request_id);
 await p.reply(second,record({identifier:'m-two',kind:'knowledge'}));
 const accounts=p.latest();
 await p.reply(accounts,targetsPage([target('qq:20002')]));
 await newer;
 await p.flush(3);
 assert.match(p.text(detailBox(p)),/知识 · m-two/);
 await p.reply(first,record());
 await older;
 await p.flush(2);
 assert.match(p.text(detailBox(p)),/知识 · m-two/);
 assert.equal(p.text(detailBox(p)).includes('记忆 · m-ok'),false);
});

test('registered accounts are read once, can be paged and show when the list ends',async()=>{
 const p=await ready();
 const job=p.buttons(p.node('sharing-items'))[0].onclick();
 const detailRequest=p.latest();
 await p.reply(detailRequest,record());
 // The first record read asks for the registered accounts exactly once.
 const accounts=p.latest();
 assert.equal(accounts.operation,'sharing.targets');
 assert.deepEqual(JSON.parse(JSON.stringify(accounts.arguments)),{after:''});
 await p.reply(accounts,targetsPage([target('qq:20002')],'qq:20002'));
 await job;
 await p.flush(3);
 assert.deepEqual(fields(p)[0].children.map(option=>option.value),['qq:20002']);
 assert.match(fields(p)[0].children[0].textContent,/QQ · qq:20002/);
 // A second page is appended, not replaced.
 p.buttons(detailBox(p),'读取更多已登记账号')[0].onclick();
 const more=p.latest();
 assert.equal(more.operation,'sharing.targets');
 assert.deepEqual(JSON.parse(JSON.stringify(more.arguments)),{after:'qq:20002'});
 await p.reply(more,targetsPage([target('qq:30003')]));
 await p.flush(3);
 assert.deepEqual(fields(p)[0].children.map(option=>option.value),['qq:20002','qq:30003']);
 assert.equal(p.buttons(detailBox(p),'已经读完全部已登记账号')[0].disabled,true);
 // Selecting another record does not read the same accounts again.
 p.node('sharing-load').onclick();
 await p.reply(p.latest(),listing([item()]));
 await p.flush(2);
 await openRow(p,0,record());
 assert.equal(p.count('sharing.targets'),2);
 assert.deepEqual(fields(p)[0].children.map(option=>option.value),['qq:20002','qq:30003']);
});

test('a failed account read is reported beside the dropdown and can be retried',async()=>{
 const p=await ready();
 const job=p.buttons(p.node('sharing-items'))[0].onclick();
 await p.reply(p.latest(),record());
 const accounts=p.latest();
 assert.equal(accounts.operation,'sharing.targets');
 await p.fail(accounts,'unknown_actor','已登记账号未能读取，请稍后重试。');
 await job;
 await p.flush(3);
 const text=p.text(detailBox(p));
 assert.match(text,/已登记账号未能读取，请稍后重试/);
 assert.match(text,/重新读取已登记账号/);
 assert.match(text,/还没有读取到已登记账号/);
 const retry=p.buttons(detailBox(p),'重新读取已登记账号')[0];
 assert.equal(retry.disabled,false);
 retry.onclick();
 const again=p.latest();
 assert.equal(again.operation,'sharing.targets');
 assert.deepEqual(JSON.parse(JSON.stringify(again.arguments)),{after:''});
 await p.reply(again,targetsPage([target('qq:20002')]));
 await p.flush(3);
 assert.deepEqual(fields(p)[0].children.map(option=>option.value),['qq:20002']);
 assert.equal(p.text(detailBox(p)).includes('还没有读取到已登记账号'),false);
});

test('grant validation stops before any request and reports beside the buttons',async()=>{
 const p=await openRow(await ready(),0);
 const before=p.requests.length;
 // No account chosen at all.
 fields(p)[0].value='';
 p.buttons(detailBox(p),'预览这次授权')[0].onclick();
 await p.flush(2);
 assert.equal(p.requests.length,before);
 assert.match(p.text(detailBox(p)),/请选择一个已登记的目标账号/);
 assert.match(p.text(detailBox(p)),/请先修正上方标出的项目/);
 // No owner.
 fields(p)[0].value='qq:20002';
 fields(p)[1].value='';
 p.buttons(detailBox(p),'预览这次授权')[0].onclick();
 await p.flush(2);
 assert.equal(p.requests.length,before);
 assert.match(p.text(detailBox(p)),/请选择资料归属账号/);
 // No purpose.
 fields(p)[1].value='qq:10001';
 const boxes=p.inputs(detailBox(p),'checkbox');
 boxes[0].checked=false;boxes[0].onchange();
 p.buttons(detailBox(p),'预览这次授权')[0].onclick();
 await p.flush(2);
 assert.equal(p.requests.length,before);
 assert.match(p.text(detailBox(p)),/至少选择一项授权用途/);
 // Bad day counts are refused locally instead of being sent.
 boxes[0].checked=true;boxes[0].onchange();
 for(const value of ['0','366','','30 天','3.5','-1']){
  p.inputs(detailBox(p),'number')[0].value=value;
  p.inputs(detailBox(p),'number')[0].oninput();
  p.buttons(detailBox(p),'预览这次授权')[0].onclick();
  await p.flush(2);
  assert.equal(p.requests.length,before,'sent a preview for days='+JSON.stringify(value));
  assert.match(p.text(detailBox(p)),/有效期必须/);
 }
 // A valid form finally asks the server, with the selected purposes.
 p.inputs(detailBox(p),'number')[0].value='30';
 p.inputs(detailBox(p),'number')[0].oninput();
 p.buttons(detailBox(p),'预览这次授权')[0].onclick();
 await p.flush(2);
 assert.equal(p.requests.length,before+1);
 const request=p.latest();
 assert.equal(request.operation,'sharing.preview_grant');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {kind:'memories',identifier:'m-ok',grantee:'qq:20002',owner:'qq:10001',purposes:['reply'],days:30});
 assert.deepEqual(p.markup,[]);
});

test('a maintenance-only grant is never presented as usable for replies',async()=>{
 const p=await openRow(await ready(),0);
 // What each purpose means is stated before anything is submitted.
 assert.match(p.text(detailBox(p)),/“维护”用途只供维护流程使用、不能用于回复/);
 const labels=p.tags(detailBox(p),'label').map(node=>node.textContent).join('|');
 assert.match(labels,/回复（可在私聊回复中使用）/);
 assert.match(labels,/维护（仅用于资料维护）/);
 // Request only the maintenance purpose.
 const boxes=p.inputs(detailBox(p),'checkbox');
 boxes[0].checked=false;boxes[0].onchange();
 boxes[1].checked=true;boxes[1].onchange();
 p.buttons(detailBox(p),'预览这次授权')[0].onclick();
 const request=p.latest();
 assert.equal(request.operation,'sharing.preview_grant');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments.purposes)),['maintenance']);
 // A preview writes nothing.
 assert.equal(p.count('sharing.commit'),0);
 await p.reply(request,grantPreview({
  after:{grants:1,grant:{grantee:'qq:20002',owner:'qq:10001',purposes:['maintenance'],
   purpose_labels:['维护'],days:30,expires_at:1800000000,target:'QQ · 20002'}},
  effect:[effect({purpose:'maintenance',purpose_label:'维护',allowed:true})],
  configured_purposes:['maintenance'],will_apply:true}));
 await p.flush(3);
 const preview=p.text(previewBox(p));
 assert.equal(p.node('sharing-preview-area').hidden,false);
 assert.match(preview,/归属账号私聊 · 维护：可以读取/);
 assert.match(preview,/是否可用于回复：这次操作不包含“回复”用途，因此不能用于回复/);
 assert.match(preview,/变更后：按服务端核对，列出的场景都能读取/);
 // "it can be read for maintenance" must not turn into "it can be used in replies".
 assert.equal(preview.includes('该账号现在可以在私聊回复中使用它'),false);
 // Requesting both purposes still never claims a reply when that very check is denied.
 p.buttons(detailBox(p),'预览这次授权')[0].onclick();
 await p.reply(p.latest(),grantPreview({
  configured_purposes:['reply','maintenance'],
  effect:[effect({purpose:'maintenance',purpose_label:'维护',allowed:true}),
   effect({allowed:false})],
  will_apply:true}));
 await p.flush(3);
 const mixed=p.text(previewBox(p));
 assert.match(mixed,/归属账号私聊 · 维护：可以读取/);
 assert.match(mixed,/归属账号私聊 · 回复：不能读取/);
 assert.match(mixed,/是否可用于回复：这次操作包含“回复”用途，但按当前状态仍不能读取/);
 assert.match(mixed,/只有部分场景能读取，其余仍会被拒绝/);
 assert.equal(mixed.includes('该账号现在可以在私聊回复中使用它'),false);
 assert.equal(p.count('sharing.commit'),0);
 assert.deepEqual(p.markup,[]);
});

test('confirm submits once, then re-reads the record and reports completion apart from readability',async()=>{
 const p=await openRow(await ready(),0,record({grants:[grantRow()]}));
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 await p.reply(p.latest(),revokePreview());
 await p.flush(3);
 const previewId=p.latest().request_id;
 const confirm=p.buttons(previewBox(p),'确认提交')[0];
 // Two clicks on the same button must produce exactly one write request.
 confirm.onclick();
 confirm.onclick();
 await p.flush(2);
 assert.equal(p.count('sharing.commit'),1);
 const commit=p.latest();
 assert.equal(commit.operation,'sharing.commit');
 assert.deepEqual(JSON.parse(JSON.stringify(commit.arguments)),{token:'tok-revoke'});
 // The write uses its own request id: a new id would be a new intent.
 assert.notEqual(commit.request_id,previewId);
 await p.reply(commit,{action:'revoke',resource:'memories/m-ok',grant_id:'g1',revoked:true,
  changed:true,effective:false,
  resources:['memories/m-ok','memories/m-other'],
  effect:[effect({shape:'public_read',label:'公开场合',allowed:true})],
  remaining_readable:['公开场合 · 回复'],
  notice:'已撤销这一条授权，并同时影响这条授权覆盖的全部资料。'});
 await p.flush(4);
 // "The revocation finished" is reported apart from "the account can still read it".
 const status=p.node('sharing-status').textContent;
 assert.match(status,/操作完成：已撤销这条授权/);
 assert.match(status,/本次撤销覆盖 2 条资料：memories\/m-ok、memories\/m-other/);
 assert.match(status,/剩余可读性：这个账号仍能通过其他依据读取其中 1 项/);
 assert.match(status,/撤销这一条授权本身已经完成，这不代表撤销失败/);
 assert.match(status,/服务端说明：已撤销这一条授权/);
 assert.equal(p.node('sharing-preview-area').hidden,true);
 // The record is re-read, so the page shows the state the server really has now.
 const reread=p.latest();
 assert.equal(reread.operation,'sharing.record');
 assert.deepEqual(JSON.parse(JSON.stringify(reread.arguments)),
  {kind:'memories',identifier:'m-ok'});
 await p.reply(reread,record({grants:[grantRow({revoked:true,active:false})],grants_active:0}));
 await p.flush(3);
 assert.match(p.text(detailBox(p)),/QQ · 20002 · 已撤销/);
 assert.match(p.text(detailBox(p)),/任何用途都不能再通过它读取这条资料/);
 assert.deepEqual(p.markup,[]);
});

test('a revocation with nothing left readable says so without claiming failure',async()=>{
 const p=await openRow(await ready(),0,record({grants:[grantRow()]}));
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 await p.reply(p.latest(),revokePreview());
 await p.flush(3);
 p.buttons(previewBox(p),'确认提交')[0].onclick();
 await p.reply(p.latest(),{action:'revoke',resource:'memories/m-ok',grant_id:'g1',revoked:true,
  changed:true,effective:false,
  effect:[effect({allowed:false})],
  notice:'已撤销这一条授权。该账号不能再通过这条授权使用这条资料；其他授权与回复授权保持原样。'});
 await p.flush(4);
 const status=p.node('sharing-status').textContent;
 assert.match(status,/操作完成：已撤销这条授权/);
 assert.match(status,/剩余可读性：这些场景现在都不能读取这条资料/);
 // Nothing here may claim the revocation itself failed.
 assert.equal(status.includes('撤销失败'),false);
 assert.equal(p.count('sharing.commit'),1);
});

test('a commit timeout reports an unknown outcome, never retries and re-reads the record',async()=>{
 const p=await openRow(await ready(),0,record({grants:[grantRow()]}));
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 await p.reply(p.latest(),revokePreview());
 await p.flush(3);
 p.buttons(previewBox(p),'确认提交')[0].onclick();
 await p.flush(2);
 assert.equal(p.latest().operation,'sharing.commit');
 p.fireTimers();
 await p.flush(4);
 const status=p.node('sharing-status').textContent;
 assert.match(status,/提交超时/);
 assert.match(status,/写入结果未知/);
 assert.match(status,/没有自动重试/);
 // Exactly one write request was ever posted, and the page then reads the record.
 assert.equal(p.count('sharing.commit'),1);
 const reread=p.latest();
 assert.equal(reread.operation,'sharing.record');
 assert.equal(p.node('sharing-preview-area').hidden,true);
 await p.reply(reread,record({grants:[grantRow()]}));
 await p.flush(3);
 // A successful re-read makes the page editable again, but never resubmits by itself.
 assert.equal(p.count('sharing.commit'),1);
 assert.equal(p.buttons(detailBox(p),'预览撤销这条授权')[0].disabled,false);
});

test('an expired preview marks the page stale and only a fresh read clears it',async()=>{
 const p=await openRow(await ready(),0,record({grants:[grantRow()]}));
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 await p.reply(p.latest(),revokePreview());
 await p.flush(3);
 p.buttons(previewBox(p),'确认提交')[0].onclick();
 await p.fail(p.latest(),'sharing_preview_expired','预览已过期或管理会话已改变，请重新预览。');
 await p.flush(4);
 const status=p.node('sharing-status').textContent;
 assert.match(status,/预览已过期或管理会话已改变/);
 assert.match(status,/不会自动重新提交/);
 assert.equal(p.count('sharing.commit'),1);
 const reread=p.latest();
 assert.equal(reread.operation,'sharing.record');
 await p.reply(reread,record({grants:[grantRow()]}));
 await p.flush(3);
 assert.equal(p.buttons(detailBox(p),'预览撤销这条授权')[0].disabled,false);
 assert.equal(p.node('sharing-preview-area').hidden,true);
});

test('cancel writes nothing, sends the token once and a second click is a no-op',async()=>{
 const p=await openRow(await ready(),0);
 p.buttons(detailBox(p),'预览这次授权')[0].onclick();
 await p.reply(p.latest(),grantPreview());
 await p.flush(3);
 const cancel=p.buttons(previewBox(p),'取消这次预览')[0];
 cancel.onclick();
 cancel.onclick();
 await p.flush(2);
 assert.equal(p.count('sharing.cancel'),1);
 const sent=p.requests.filter(entry=>entry.operation==='sharing.cancel')[0];
 assert.deepEqual(JSON.parse(JSON.stringify(sent.arguments)),{token:'tok-grant'});
 assert.equal(p.count('sharing.commit'),0);
 assert.equal(p.node('sharing-preview-area').hidden,true);
 assert.match(p.node('sharing-status').textContent,/已取消这次预览，没有写入任何更改/);
 // A late cancel reply cannot bring the preview back.
 await p.reply(sent,{cancelled:true,action:'grant',written:false});
 await p.flush(2);
 assert.equal(p.node('sharing-preview-area').hidden,true);
});

test('a revoke preview lists the complete scope, beyond the truncated grant summary',async()=>{
 const all=numbered('covered',41);
 const summary=all.slice(0,30);
 const p=await openRow(await ready(),0,
  record({grants:[grantRow({resource_count:41,resources:summary})]}));
 // The detail view names the truncation instead of pretending the 30 shown are all.
 const detail=p.text(detailBox(p));
 assert.match(detail,/这条授权覆盖 41 条资料/);
 assert.match(detail,/这里只显示其中前 30 条（已截断），完整范围以撤销预览为准/);
 assert.match(detail,/撤销会同时影响这些资料/);
 // The revocation preview is authoritative: every covered record is listed.
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 const request=p.latest();
 assert.equal(request.operation,'sharing.preview_revoke');
 await p.reply(request,revokePreview({
  resources:all,
  before:{grants:1,grant:grantRow({resource_count:41,resources:summary})}}));
 await p.flush(3);
 const text=p.text(previewBox(p));
 assert.match(text,/确认撤销授权/);
 assert.match(text,/撤销对象：QQ · 20002/);
 assert.match(text,/这条授权的状态：有效/);
 assert.match(text,/撤销范围（整条授权的全部资料）/);
 assert.match(text,/这条授权覆盖 41 条资料，下面逐条列出/);
 // Including the 31st resource, which the truncated summary never contained.
 for(const resource of [all[0],all[29],all[30],all[40]])assert.equal(text.includes(resource),true,resource);
 assert.equal(summary.includes(all[40]),false);
 const listed=p.tags(previewBox(p),'li').filter(node=>node.textContent.includes('现存依据：'));
 assert.equal(listed.length,41);
 // Nothing claims the revocation touches only the record being viewed.
 assert.equal(text.includes('只影响当前这条资料'),false);
 assert.equal(text.includes('只影响这条资料'),false);
 assert.equal(p.buttons(previewBox(p),'确认提交')[0].disabled,false);
});

test('downstream dependents show the count, the list and an explicit truncation note',async()=>{
 const deps=numbered('dependent',50);
 const p=await openRow(await ready(),0,record({grants:[grantRow()]}));
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 await p.reply(p.latest(),revokePreview({scope_dependents:deps,
  scope_dependents_count:'> 50',scope_dependents_limited:true}));
 await p.flush(3);
 const text=p.text(previewBox(p));
 assert.match(text,/下游依赖：> 50 条资料以这条授权覆盖的资料为来源/);
 assert.match(text,/条资料以这条授权覆盖的资料为来源/);
 for(const resource of [deps[0],deps[49]])assert.equal(text.includes(resource),true,resource);
 assert.match(text,/下游依赖超过展示上限，只列出前 50 条/);
 assert.match(text,/撤销只改这一条授权，不会改动下游资料本身/);
 // A complete, small dependent list is reported without any truncation wording.
 const q=await openRow(await ready(),0,record({grants:[grantRow()]}));
 q.buttons(detailBox(q),'预览撤销这条授权')[0].onclick();
 await q.reply(q.latest(),revokePreview({scope_dependents:['memories/dep-a','memories/dep-b'],
  scope_dependents_count:2}));
 await q.flush(3);
 const complete=q.text(previewBox(q));
 assert.match(complete,/下游依赖：2 条资料以这条授权覆盖的资料为来源/);
 assert.match(complete,/下游资料：memories\/dep-a、memories\/dep-b/);
 assert.equal(complete.includes('超过展示上限'),false);
});

test('a surviving basis is reported as a basis, never as a final readability claim',async()=>{
 const entries=[
  scopeEntry({resource:'memories/m-public',remaining_basis:'public'}),
  scopeEntry({resource:'memories/m-gone',present:false,policy:null,remaining_basis:'missing'}),
  scopeEntry({resource:'memories/m-second',remaining_basis:'other_grant',other_grants:7,grantee_grants_active:2}),
  scopeEntry({resource:'memories/m-odd',remaining_basis:'strange_state'})];
 const p=await openRow(await ready(),0,record({grants:[grantRow()]}));
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 await p.reply(p.latest(),revokePreview({resources:entries.map(entry=>entry.resource),
  scope:scopeOf(entries)}));
 await p.flush(3);
 const text=p.text(previewBox(p));
 assert.match(text,/现存依据：分类公开，不依赖这条授权/);
 assert.match(text,/现存依据：权限记录缺失/);
 assert.match(text,/现存依据：还有另一条有效授权/);
 assert.match(text,/该账号另有 2 条当前有效授权/);
 assert.match(text,/其他授权记录共 7 条（包括历史记录及其他账号）/);
 assert.doesNotMatch(text,/7 条有效授权/);
 assert.match(text,/现存依据：未识别的依据类型（strange_state）/);
 assert.match(text,/资料本体已不存在/);
 assert.match(text,/权限记录缺失，无法核对这条资料的现状/);
 // The backend's old "still readable" labels are not quoted any more: the page states
 // each surviving basis itself and adds its own qualifier.
 assert.equal(text.includes('撤销后仍可读'),false);
 assert.equal(text.includes('该账号是归属账号，撤销后仍可读'),false);
 assert.equal(text.includes('在服务端的说明'),false);
 assert.match(text,/以上都只是现存依据；实际读取仍须通过来源检查/);
 // No per-record line may state readability as a result of this revocation.
 const basis=p.tags(previewBox(p),'li').filter(node=>node.textContent.includes('现存依据：'));
 assert.equal(basis.length,4);
 for(const line of basis){
  assert.equal(line.textContent.includes('仍可读'),false,line.textContent);
  assert.equal(line.textContent.includes('可以读取'),false,line.textContent);
 }
});

test('a revocation without a complete scope cannot be confirmed, and other actions stay usable',async()=>{
 const p=await openRow(await ready(),0,record({grants:[grantRow()]}));
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 await p.reply(p.latest(),revokePreview({scope:null,scope_count:undefined}));
 await p.flush(3);
 const text=p.text(previewBox(p));
 assert.match(text,/没有完整撤销范围/);
 assert.match(text,/这次预览没有返回撤销范围/);
 assert.match(text,/不让你在看不到全部受影响资料时提交/);
 const confirm=p.buttons(previewBox(p),'确认提交')[0];
 assert.equal(confirm.disabled,true);
 assert.match(text,/“确认提交”已禁用：这次预览没有完整的撤销范围/);
 // Clicking the disabled confirm writes nothing.
 confirm.onclick();
 await p.flush(3);
 assert.equal(p.count('sharing.commit'),0);
 assert.match(p.node('sharing-status').textContent,/因此没有提交任何更改/);
 // The cancel button still works.
 const cancel=p.buttons(previewBox(p),'取消这次预览')[0];
 assert.equal(cancel.disabled,false);
 cancel.onclick();
 await p.flush(2);
 assert.equal(p.count('sharing.cancel'),1);
 assert.equal(p.node('sharing-preview-area').hidden,true);
 // Other actions stay usable after the refusal: a grant preview still works.
 p.buttons(detailBox(p),'预览这次授权')[0].onclick();
 const grant=p.latest();
 assert.equal(grant.operation,'sharing.preview_grant');
 await p.reply(grant,grantPreview());
 await p.flush(3);
 assert.equal(p.buttons(previewBox(p),'确认提交')[0].disabled,false);
});

test('a scope count that disagrees with the listed entries also blocks confirmation',async()=>{
 const p=await openRow(await ready(),0,record({grants:[grantRow()]}));
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 await p.reply(p.latest(),revokePreview({
  scope:scopeOf([scopeEntry({resource:'memories/m-ok'})],{count:5}),
  scope_count:5}));
 await p.flush(3);
 const text=p.text(previewBox(p));
 assert.match(text,/没有完整撤销范围/);
 assert.match(text,/服务端标记的范围条数（5）与返回的条目数（1）不一致/);
 assert.equal(p.buttons(previewBox(p),'确认提交')[0].disabled,true);
 // A scope entry without a resource id is refused the same way.
 const q=await openRow(await ready(),0,record({grants:[grantRow()]}));
 q.buttons(detailBox(q),'预览撤销这条授权')[0].onclick();
 await q.reply(q.latest(),revokePreview({
  scope:scopeOf([scopeEntry({resource:''})],{count:1}),scope_count:1}));
 await q.flush(3);
 assert.match(q.text(previewBox(q)),/撤销范围里有条目缺少资料标识/);
 assert.equal(q.buttons(previewBox(q),'确认提交')[0].disabled,true);
});

test('an already expired grant is described as a cleanup, not as a scope change',async()=>{
 const p=await openRow(await ready(),0,record({grants:[grantRow({active:false,expired:true})]}));
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 await p.reply(p.latest(),revokePreview({
  before:{grants:1,grant:grantRow({active:false,expired:true})},after:{grants:1}}));
 await p.flush(3);
 const text=p.text(previewBox(p));
 assert.match(text,/这条授权的状态：已失效（已过期）/);
 assert.match(text,/撤销它不会减少当前生效的授权数量/);
 assert.match(text,/这条授权覆盖 1 条资料，下面逐条列出/);
});

test('a scope with a duplicate resource is refused even when every count matches',async()=>{
 const p=await openRow(await ready(),0,record({grants:[grantRow()]}));
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 const entries=[scopeEntry({resource:'memories/m-ok'}),scopeEntry({resource:'memories/m-ok'})];
 await p.reply(p.latest(),revokePreview({resources:['memories/m-ok'],
  scope:scopeOf(entries),scope_count:2}));
 await p.flush(3);
 const text=p.text(previewBox(p));
 assert.match(text,/没有完整撤销范围/);
 assert.match(text,/撤销范围里有重复的资料（memories\/m-ok）/);
 assert.equal(p.buttons(previewBox(p),'确认提交')[0].disabled,true);
});

test('two disagreeing scope counts block confirmation even when the list length matches one of them',async()=>{
 const p=await openRow(await ready(),0,record({grants:[grantRow()]}));
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 await p.reply(p.latest(),revokePreview({
  scope:scopeOf([scopeEntry({resource:'memories/m-ok'})],{count:1}),scope_count:2}));
 await p.flush(3);
 const text=p.text(previewBox(p));
 assert.match(text,/服务端两处标记的范围条数不一致（scope_count=2，count=1）/);
 assert.equal(p.buttons(previewBox(p),'确认提交')[0].disabled,true);
 // A non-numeric marker is refused too: it cannot be verified.
 const q=await openRow(await ready(),0,record({grants:[grantRow()]}));
 q.buttons(detailBox(q),'预览撤销这条授权')[0].onclick();
 await q.reply(q.latest(),revokePreview({
  scope:scopeOf([scopeEntry({resource:'memories/m-ok'})]),scope_count:'1'}));
 await q.flush(3);
 assert.match(q.text(previewBox(q)),/scope_count 不是有效的条数/);
 assert.equal(q.buttons(previewBox(q),'确认提交')[0].disabled,true);
});

test('a record whose grants were cut short says the rest exist and are not listed',async()=>{
 const p=await openRow(await ready(),0,record({grants:[grantRow()],grants_limited:true,
  grants_active:1,grants_total:3,grants_revoked:2}));
 const text=p.text(detailBox(p));
 assert.match(text,/服务端统计：这条资料现在有 1 条有效授权（共有 3 条授权记录，其中已撤销 2 条/);
 assert.match(text,/只列出当前有效的授权/);
 assert.match(text,/服务端标记这份授权列表被截断，却没有给出下一页位置/);
 assert.match(text,/其余授权仍然有效/);
 assert.equal(text.includes('授权分页仍待后端提供接口'),false);
});

test('the 51st grant is reachable from the next page and can be revoked there',async()=>{
 const p=await openRow(await ready(),0,record({grants:grantList(50,1),grants_next:'g50',
  grants_limited:true,grants_active:51,grants_total:51}));
 const text=p.text(detailBox(p));
 assert.match(text,/服务端统计：这条资料现在有 51 条有效授权（共有 51 条授权记录/);
 assert.equal(p.tags(detailBox(p),'article').length,50);
 assert.equal(text.includes('QQ · 20051'),false);
 assert.equal(p.buttons(detailBox(p),'上一页授权')[0].disabled,true);
 assert.equal(p.buttons(detailBox(p),'下一页授权')[0].disabled,false);
 assert.match(text,/当前第 1 页，本页 50 条；还有下一页/);
 // The second page is one bounded page, read with the cursor the detail reported.
 p.buttons(detailBox(p),'下一页授权')[0].onclick();
 const request=p.latest();
 assert.equal(request.operation,'sharing.grants');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {kind:'memories',identifier:'m-ok',after:'g50'});
 await p.reply(request,{kind:'memories',identifier:'m-ok',grants:grantList(1,51),next:null,
  count:1,limit:50,counts:{active:51,total:51,revoked:0,stale_epoch:0}});
 await p.flush(3);
 const second=p.text(detailBox(p));
 assert.match(second,/已有私聊授权（第 2 页）/);
 assert.equal(p.tags(detailBox(p),'article').length,1);
 assert.match(second,/QQ · 20051/);
 assert.equal(second.includes('QQ · 20001'),false);
 assert.match(second,/当前第 2 页，本页 1 条；已经是最后一页/);
 assert.equal(p.buttons(detailBox(p),'下一页授权')[0].disabled,true);
 assert.equal(p.buttons(detailBox(p),'上一页授权')[0].disabled,false);
 // The 51st grant can be previewed and confirmed from this page.
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 const preview=p.latest();
 assert.equal(preview.operation,'sharing.preview_revoke');
 assert.deepEqual(JSON.parse(JSON.stringify(preview.arguments)),
  {kind:'memories',identifier:'m-ok',grant_id:'g51'});
 await p.reply(preview,revokePreview({
  before:{grants:1,grant:grantRow({id:'g51',target:'QQ · 20051'})}}));
 await p.flush(3);
 assert.match(p.text(previewBox(p)),/撤销对象：QQ · 20051/);
 assert.equal(p.buttons(previewBox(p),'确认提交')[0].disabled,false);
});

test('a late grant page cannot paint the grants of another record',async()=>{
 const p=await ready([item(),item({identifier:'m-two',label:'记忆 · m-two'})]);
 const rows=p.buttons(p.node('sharing-items'));
 const first=rows[0].onclick();
 await p.reply(p.latest(),record({grants:grantList(50,1),grants_next:'g50',grants_active:50,
  grants_total:50}));
 await p.reply(p.latest(),targetsPage([target('qq:20002')]));
 await first;
 await p.flush(3);
 // Page two of the first record is requested and stays pending.
 p.buttons(detailBox(p),'下一页授权')[0].onclick();
 const late=p.latest();
 assert.equal(late.operation,'sharing.grants');
 // Meanwhile another record is selected and read.
 const second=p.buttons(p.node('sharing-items'))[1].onclick();
 await p.reply(p.latest(),record({identifier:'m-two',kind:'knowledge',grants:grantList(1,900),
  grants_active:1,grants_total:1}));
 await second;
 await p.flush(3);
 assert.match(p.text(detailBox(p)),/知识 · m-two/);
 assert.match(p.text(detailBox(p)),/QQ · 20900/);
 // The late page arrives last and must not repaint the record or its page.
 await p.reply(late,{grants:grantList(1,777),next:null,count:1});
 await p.flush(3);
 const text=p.text(detailBox(p));
 assert.match(text,/知识 · m-two/);
 assert.match(text,/QQ · 20900/);
 assert.equal(text.includes('QQ · 20777'),false);
 assert.equal(text.includes('第 2 页'),false);
});

test('a failed grant page read keeps the current page and can be retried',async()=>{
 const p=await openRow(await ready(),0,record({grants:grantList(50,1),grants_next:'g50',
  grants_active:51,grants_total:51}));
 p.buttons(detailBox(p),'下一页授权')[0].onclick();
 await p.fail(p.latest(),'sharing_query','授权分页位置无效，请重新读取。');
 await p.flush(3);
 const failed=p.text(detailBox(p));
 assert.match(failed,/授权分页位置无效，请重新读取/);
 assert.match(failed,/当前仍停在第 1 页/);
 assert.match(failed,/可以再点“下一页授权”重试/);
 // The old page is not relabelled as the page that failed.
 assert.equal(failed.includes('第 2 页'),false);
 assert.match(failed,/当前第 1 页，本页 50 条；还有下一页/);
 assert.equal(p.tags(detailBox(p),'article').length,50);
 // The same button retries the same cursor and only then lands on page two.
 p.buttons(detailBox(p),'下一页授权')[0].onclick();
 const again=p.latest();
 assert.equal(again.operation,'sharing.grants');
 assert.deepEqual(JSON.parse(JSON.stringify(again.arguments)),
  {kind:'memories',identifier:'m-ok',after:'g50'});
 await p.reply(again,{grants:grantList(1,51),next:null,count:1});
 await p.flush(3);
 assert.match(p.text(detailBox(p)),/已有私聊授权（第 2 页）/);
 assert.equal(p.tags(detailBox(p),'article').length,1);
 assert.equal(p.text(detailBox(p)).includes('当前仍停在第 1 页'),false);
});

test('going back to the first grant page reuses the detail snapshot and a confirmed change resets paging',async()=>{
 const p=await openRow(await ready(),0,record({grants:grantList(50,1),grants_next:'g50',
  grants_active:51,grants_total:51}));
 p.buttons(detailBox(p),'下一页授权')[0].onclick();
 await p.reply(p.latest(),{grants:grantList(1,51),next:null,count:1});
 await p.flush(3);
 assert.match(p.text(detailBox(p)),/已有私聊授权（第 2 页）/);
 // Going back needs no request: page one is the detail snapshot itself.
 const reads=p.count('sharing.grants');
 p.buttons(detailBox(p),'上一页授权')[0].onclick();
 await p.flush(2);
 assert.equal(p.count('sharing.grants'),reads);
 assert.equal(p.text(detailBox(p)).includes('第 2 页'),false);
 assert.equal(p.tags(detailBox(p),'article').length,50);
 assert.equal(p.buttons(detailBox(p),'上一页授权')[0].disabled,true);
 // Revoke the 51st from page two, confirm it, and the re-read detail restarts at page one.
 p.buttons(detailBox(p),'下一页授权')[0].onclick();
 await p.reply(p.latest(),{grants:grantList(1,51),next:null,count:1});
 await p.flush(3);
 p.buttons(detailBox(p),'预览撤销这条授权')[0].onclick();
 await p.reply(p.latest(),revokePreview({before:{grants:1,grant:grantRow({id:'g51'})}}));
 await p.flush(3);
 p.buttons(previewBox(p),'确认提交')[0].onclick();
 const commit=p.latest();
 assert.equal(commit.operation,'sharing.commit');
 await p.reply(commit,{action:'revoke',resource:'memories/m-ok',grant_id:'g51',revoked:true,
  changed:true,resources:['memories/m-ok'],effect:[effect({allowed:false})],
  notice:'已撤销这一条授权，并同时影响这条授权覆盖的全部资料。'});
 await p.flush(4);
 const reread=p.latest();
 assert.equal(reread.operation,'sharing.record');
 await p.reply(reread,record({grants:grantList(50,1),grants_next:'g50',grants_active:50,
  grants_total:50}));
 await p.flush(3);
 const text=p.text(detailBox(p));
 assert.equal(text.includes('第 2 页'),false);
 assert.match(text,/当前第 1 页，本页 50 条；还有下一页/);
 assert.equal(p.buttons(detailBox(p),'上一页授权')[0].disabled,true);
 assert.equal(p.buttons(detailBox(p),'下一页授权')[0].disabled,false);
});

test('a classify preview never derives widening or narrowing from a rank order',async()=>{
 const p=await openRow(await ready(),0);
 const choice=fields(p)[2];
 choice.value='restricted';choice.onchange();
 p.buttons(detailBox(p),'预览这次分类更改')[0].onclick();
 const request=p.latest();
 assert.equal(request.operation,'sharing.preview_classify');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {kind:'memories',identifier:'m-ok',classification:'restricted'});
 await p.reply(request,classifyPreview());
 await p.flush(3);
 const text=p.text(previewBox(p));
 assert.match(text,/原分类：仅归属账号私聊（personal）/);
 assert.match(text,/新分类：仅显式授权（默认无人可读）（restricted）/);
 assert.match(text,/保持原样：归属账号 qq:10001/);
 assert.match(text,/服务端标记的方向：收窄/);
 assert.match(text,/分类之间没有单一总序/);
 assert.match(text,/本页不按分类顺序推断扩大或收窄/);
 assert.match(text,/归属账号私聊 · 回复：不能读取/);
 assert.equal(p.count('sharing.commit'),0);
});

test('an unchanged classification is refused before any request',async()=>{
 const p=await openRow(await ready(),0);
 const before=p.requests.length;
 // The select defaults to the record's current classification.
 p.buttons(detailBox(p),'预览这次分类更改')[0].onclick();
 await p.flush(2);
 assert.equal(p.requests.length,before);
 assert.match(p.text(detailBox(p)),/新分类与当前分类相同，没有需要提交的更改/);
});

test('locking clears a typed draft and an open preview',async()=>{
 const p=await openRow(await ready(),0);
 p.inputs(detailBox(p),'number')[0].value='9';
 p.inputs(detailBox(p),'number')[0].oninput();
 p.buttons(detailBox(p),'预览这次授权')[0].onclick();
 await p.reply(p.latest(),grantPreview());
 await p.flush(3);
 assert.equal(p.node('sharing-preview-area').hidden,false);
 p.state(true,false);
 assert.equal(p.text(detailBox(p)),'');
 assert.equal(p.node('sharing-preview-area').hidden,true);
 // The late preview reply of the cancelled page must not come back either.
 await p.reply(p.requests.filter(entry=>entry.operation==='sharing.preview_grant')[0],
  grantPreview());
 await p.flush(2);
 assert.equal(p.node('sharing-preview-area').hidden,true);
 // Unlocking starts from scratch: no typed 9, no accounts kept, no preview.
 p.state(true,true);
 await p.reply(p.latest(),listing([item()]));
 await p.flush(2);
 await openRow(p,0,record());
 assert.equal(p.inputs(detailBox(p),'number')[0].value,'30');
 assert.equal(p.node('sharing-preview-area').hidden,true);
});

test('field values are rendered as text and never as markup',async()=>{
 const hostile='<img src=x onerror=alert(1)>';
 const p=await openRow(await ready([item({label:hostile,classification_label:'<b>odd</b>'})]),0,
  record({identifier:'<script>alert(2)</script>',
   policy:policyView({classification_label:hostile}),
   grants:[grantRow({target:'<svg onload=alert(3)>'})],
   source:report({ok:false,issues:[{resource:'<i>src</i>',detail:hostile}],total_issues:1}),
   blockers:['<u>blocked</u>']}));
 const list=p.text(p.node('sharing-items'));
 assert.match(list,/<img src=x onerror=alert\(1\)>/);
 assert.match(list,/<b>odd<\/b>/);
 const text=p.text(detailBox(p));
 assert.match(text,/<script>alert\(2\)<\/script>/);
 assert.match(text,/<img src=x onerror=alert\(1\)>/);
 assert.match(text,/<svg onload=alert\(3\)>/);
 assert.match(text,/· <u>blocked<\/u>/);
 assert.match(text,/来源：<i>src<\/i> — <img src=x onerror=alert\(1\)>/);
 assert.deepEqual(p.markup,[]);
});

test('paging appends the next page, is disabled without a cursor and stops at the end',async()=>{
 const p=page();
 p.state(true,true);
 const first=p.latest();
 assert.equal(first.operation,'sharing.records');
 await p.reply(first,listing([item(),item({identifier:'m-two',label:'记忆 · m-two'})],
  {next:'memories/m-two'}));
 await p.flush(2);
 assert.equal(p.node('sharing-next').disabled,false);
 p.node('sharing-next').onclick();
 const request=p.latest();
 assert.equal(request.operation,'sharing.records');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),{after:'memories/m-two',limit:20});
 await p.reply(request,listing([item({identifier:'m-three',label:'记忆 · m-three'})],
  {offset:'memories/m-two'}));
 await p.flush(2);
 const text=p.text(p.node('sharing-items'));
 assert.match(text,/记忆 · m-ok · /);
 assert.match(text,/记忆 · m-two/);
 assert.match(text,/记忆 · m-three/);
 // The last page has no cursor, so the same button can no longer send a read.
 assert.equal(p.node('sharing-next').disabled,true);
 const sent=p.count('sharing.records');
 p.node('sharing-next').onclick();
 await p.flush(2);
 assert.equal(p.count('sharing.records'),sent);
});

test('an empty first page says the machine has nothing manageable yet',async()=>{
 const p=page();
 p.state(true,true);
 await p.reply(p.latest(),listing([]));
 await p.flush(2);
 assert.match(p.text(p.node('sharing-items')),/本机还没有可以在这里调整共享范围的资料/);
 assert.match(p.text(p.node('sharing-items')),/共用人物设定与个人事实仍走各自的流程/);
 assert.equal(p.node('sharing-load').disabled,false);
 assert.equal(p.node('sharing-next').disabled,true);
 // A failed read must never be presented as this empty state.
 p.node('sharing-load').onclick();
 await p.fail(p.latest(),'sharing_backend','资料共享设置需要事务化 PostgreSQL 核心');
 await p.flush(3);
 assert.equal(p.text(p.node('sharing-items')).includes('本机还没有可以在这里调整共享范围的资料'),false);
 assert.equal(p.text(p.node('sharing-items')),
  '还没有读取可管理的资料。点“读取可管理的资料”开始。');
});
