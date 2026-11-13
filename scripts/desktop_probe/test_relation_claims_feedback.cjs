// Interpersonal claim adjudication page tests. Every value here is synthetic: nothing
// reaches a database, a platform, a model or the real desktop shell.
//
// The page runs on a tiny fake DOM plus a fake WebView host, so a test can hold one reply
// back, deliver replies out of order, fire the request timeout and click the same button
// twice. Assertions are made on what the page *shows* and on the exact operations it
// posts, never on the page source text and never on a copy of the page's own helpers.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');

function page(){
 const nodes=new Map(),events={},requests=[],listeners=[],timers=new Map(),markup=[];
 const element=tag=>{
  let own='';
  const node={tagName:tag,hidden:false,disabled:false,value:'',className:'',
   type:'',min:'',max:'',step:'',rows:0,maxLength:0,checked:false,attributes:{},children:[],
   append(...kids){for(const kid of kids)this.children.push(kid);},
   replaceChildren(...kids){this.children=kids;this.value='';},
   setAttribute(name,value){this.attributes[name]=value;},
   removeAttribute(name){delete this.attributes[name];}};
  // Like the real DOM, an element's text includes its children's text, so a label built
  // from a select plus a text node can be read back in a test.
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
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/relation-claims.js'),'utf8'),{
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
 function selects(target){return tags(target,'select');}
 function textareas(target){return tags(target,'textarea');}
 const latest=()=>requests.at(-1);
 const count=operation=>requests.filter(item=>item.operation===operation).length;
 return {node,requests,markup,state,reply,fail,settle,flush,fireTimers,texts,text,tags,buttons,
  selects,textareas,latest,count};
}

// --- synthetic backend payloads --------------------------------------------
function account(key,label){return {key,registered:Boolean(label),label:label||''};}
function claimView(overrides={}){
 return {id:'c1',cursor:'c1',status:'claimed',status_label:'仅声明（尚未核对）',version:'v1',
  queryable:true,restorable:false,subject:account('actor-a','QQ · 10001'),
  object:account('actor-b','微信 · wxid_b'),declarer:account('actor-a','QQ · 10001'),
  relation:'同事',direction:'subject_to_object',declared_reason:'合成的原因',
  evidence_count:2,...overrides};
}
function listing(claims,overrides={}){
 return {claims,count:claims.length,next:null,after:'',limit:20,page_max:50,status:'all',
  totals:{claimed:claims.length,confirmed:0,disputed:0,revoked:0},
  statuses:[{value:'claimed',label:'仅声明（尚未核对）'},{value:'confirmed',label:'已确认'},
   {value:'disputed',label:'有争议'},{value:'revoked',label:'已撤销'}],
  status_meaning:'“有争议”仍会由核心带状态返回，“已撤销”不再返回。',
  meaning:'这里只更正已有关系声明的状态。',...overrides};
}
function policyView(overrides={}){
 return {classification:'personal',owners:['actor-a'],conversation:'room-a',audience:'private',
  valid:true,expires_at:null,version:'p1',source_count:2,
  sources:['event/e1','memories/m1'],source_versions:[['event/e1','ev1'],['memories/m1','v1']],
  pins_complete:true,...overrides};
}
function evidenceItem(overrides={}){
 return {resource:'event/e1',kind:'event',pinned_version:'ev1',current_version:'ev1',
  state:'matched',state_label:'引用版本与当前一致',matched:true,truncated:false,
  page_limited:false,excerpt:'合成的消息原文',...overrides};
}
function evidence(overrides={}){
 const items=overrides.items||[evidenceItem()];
 return {items,count:items.length,limit:16,excerpt_chars:20,excerpt_length:2000,
  excerpt_total:12000,declared_count:items.length,pinned_count:items.length,
  meaning:'只有引用版本与当前一致时才展示摘录。',...overrides};
}
function report(overrides={}){
 return {ok:true,checked_at:1800000000,issues:[],total_issues:0,issues_limited:false,
  queried_as_revoked:false,meaning:'这些只是来源与有效状态的检查结果。',...overrides};
}
function impact(overrides={}){
 return {fingerprint:'fp',affected_count:1,sample:['relation/c1'],sample_limited:false,
  total_count:1,self_count:1,downstream_count:0,
  meaning:'影响范围共 1 项，其中 1 项是这条声明自身。',...overrides};
}
function readable(overrides={}){
 return {checked:true,allowed:true,actor:'QQ · 10001',
  note:'按来源与有效状态核对：声明人自己的回复仍可以把它作为依据。',...overrides};
}
function historyView(overrides={}){
 return {available:true,count:1,complete:true,current:true,
  note:'这里共记录了 1 次状态更正。',...overrides};
}
function detail(overrides={}){
 const claim=overrides.claim||claimView();
 return {claim,policy:policyView(),evidence:evidence(),source:report(),impact:impact(),
  history_available:true,history:historyView(),readable:readable(),
  check:{consistent:true,anomaly:null,detail:null},
  statuses:[{value:'claimed',label:'仅声明（尚未核对）'},{value:'confirmed',label:'已确认'},
   {value:'disputed',label:'有争议'},{value:'revoked',label:'已撤销'}],
  targets:[{value:'confirmed',label:'已确认'},{value:'disputed',label:'有争议'},
   {value:'revoked',label:'已撤销'}],
  status_meaning:'“有争议”仍会由核心带状态返回，“已撤销”不再返回。',
  evidence_limit:16,excerpt_limit:2000,excerpt_page_limit:12000,history_limit:20,
  meaning:'这里只更正已有关系声明的状态。',...overrides};
}
function historyItem(overrides={}){
 return {cursor:'000000000001',sequence:1,at:1800000000,unreadable:false,
  actor:'actor-owner',actor_label:'本机 · 管理员',reason:'合成说明',restored:false,
  before:{status:'claimed',status_label:'仅声明（尚未核对）',version:'v1',
   policy_version:'p1',valid:true},
  after:{status:'confirmed',status_label:'已确认',version:'v2',policy_version:'v2',valid:true},
  claim_snapshot:{subject:'actor-a',object:'actor-b',declarer:'actor-a',status:'claimed',
   version:'v1',relation:'同事',declared_reason:'合成的原因',evidence:['event/e1'],
   unreadable:false},
  sources:[['event/e1','ev1']],...overrides};
}
function historyPage(items,next){
 return {identifier:'c1',items,next:next||null,count:items.length,after:'',limit:20,
  history:historyView({count:items.length}),
  statuses:[{value:'claimed',label:'仅声明（尚未核对）'},{value:'confirmed',label:'已确认'}],
  meaning:'这里按写入顺序列出每一次状态更正。'};
}
function previewOf(overrides={}){
 const status=overrides.status||'confirmed';
 const claim=overrides.claim||claimView();
 return {token:'tok-1',action:'adjudicate',identifier:'c1',resource:'relation/c1',claim,
  before:{status:'claimed',status_label:'仅声明（尚未核对）',version:'v1',
   policy_version:'p1',valid:true,queryable:true},
  after:{status,status_label:status==='revoked'?'已撤销':'已确认',valid:status!=='revoked',
   queryable:status!=='revoked',restores:false},
  restored:false,reason:'',reason_limit:1000,source:report(),impact:impact(),
  readable_before:readable(),history:historyView(),status_meaning:'状态说明',
  notice:'确认后只改变这条声明的状态，也不改账号关联、回复授权或其他资料权限。',
  written:false,...overrides};
}
function committed(overrides={}){
 return {action:'adjudicate',identifier:'c1',resource:'relation/c1',status:'confirmed',
  status_label:'已确认',version:'v2',changed:true,state_changed:true,
  before:{status:'claimed',status_label:'仅声明（尚未核对）',version:'v1',policy_version:'p1'},
  after:{status:'confirmed',status_label:'已确认',version:'v2',policy_version:'v2',
   valid:true,queryable:true},
  restored:false,history:{sequence:1,at:1800000000,restored:false,cursor:'1'},
  reason:'',source:report(),readable:readable(),usable:true,impact:impact(),
  notice:'状态更改成功：仅声明（尚未核对） → 已确认，并已写入一条不可变的裁决记录。',
  readable_notice:'按来源与有效状态核对：声明人自己的回复仍可以把它作为依据。',
  ...overrides};
}

// --- harness steps ---------------------------------------------------------
function itemsBox(p){return p.node('relation-claims-items');}
function detailBox(p){return p.node('relation-claims-detail');}
function historyBox(p){return p.node('relation-claims-history');}
function previewBox(p){return p.node('relation-claims-preview');}
function statusSelect(p){return p.selects(detailBox(p))[0];}

async function ready(claims){
 const p=page();
 p.state(true,true);
 const request=p.latest();
 assert.equal(request.operation,'relations.list');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {after:'',limit:20,status:'all'});
 await p.reply(request,listing(claims===undefined?[claimView()]:claims));
 await p.flush(2);
 return p;
}
// Select a listed claim and answer its detail read plus the history read it starts.
async function open(p,index,payload,historyRows){
 const rows=p.buttons(itemsBox(p));
 const job=rows[index===undefined?0:index].onclick();
 const request=p.latest();
 assert.equal(request.operation,'relations.detail');
 await p.reply(request,payload||detail());
 const pending=p.latest();
 if(pending!==request&&pending.operation==='relations.history')
  await p.reply(pending,historyPage(historyRows===undefined?[historyItem()]:historyRows));
 await job;
 await p.flush(3);
 return p;
}

test('a locked page posts nothing and unlocking reads the claim list',async()=>{
 const p=page();
 p.state(true,false);
 assert.equal(p.requests.length,0);
 p.node('relation-claims-load').onclick();
 await p.flush(2);
 assert.equal(p.requests.length,0);
 assert.equal(p.text(itemsBox(p)),'');
 p.state(true,true);
 assert.equal(p.latest().operation,'relations.list');
 await p.reply(p.latest(),listing([claimView()]));
 await p.flush(2);
 const text=p.text(itemsBox(p));
 assert.match(text,/QQ · 10001 → 微信 · wxid_b · 同事 · 仅声明（尚未核对）/);
 assert.equal(p.node('relation-claims-status').textContent,'已读取 1 条声明。');
 assert.match(text,/只有本机管理员能读取/);
 // The area follows the management panel, including when nothing else changed.
 p.node('management-panel').hidden=true;
 p.state(true,true);
 assert.equal(p.node('relation-claims-panel').hidden,true);
 p.node('management-panel').hidden=false;
 p.state(true,true);
 assert.equal(p.node('relation-claims-panel').hidden,false);
 // Locking drops the list and everything derived from it.
 p.state(true,false);
 assert.equal(p.text(itemsBox(p)),'');
 assert.equal(p.node('relation-claims-status').textContent,'');
 assert.equal(p.text(detailBox(p)),'');
 assert.equal(p.text(historyBox(p)),'');
 assert.equal(p.node('relation-claims-preview-area').hidden,true);
 assert.equal(p.node('relation-claims-load').disabled,true);
});

test('a late list reply from before a lock cannot revive the page',async()=>{
 const p=await ready();
 p.node('relation-claims-load').onclick();
 const late=p.latest();
 assert.equal(late.operation,'relations.list');
 p.state(true,false);
 await p.flush(2);
 assert.equal(p.text(itemsBox(p)),'');
 await p.reply(late,listing([claimView({id:'c-old',cursor:'c-old',relation:'旧声明'})]));
 await p.flush(3);
 assert.equal(p.text(itemsBox(p)),'');
 p.state(true,true);
 const fresh=p.latest();
 await p.reply(fresh,listing([claimView({id:'c-new',cursor:'c-new',relation:'新声明'})]));
 await p.flush(2);
 assert.match(p.text(itemsBox(p)),/新声明/);
 assert.equal(p.text(itemsBox(p)).includes('旧声明'),false);
});

test('a failed list read leaves no claim, draft or preview behind',async()=>{
 const p=await open(await ready(),0);
 p.selects(detailBox(p))[0].value='confirmed';
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.reply(p.latest(),previewOf());
 await p.flush(3);
 assert.equal(p.node('relation-claims-preview-area').hidden,false);
 p.node('relation-claims-load').onclick();
 await p.fail(p.latest(),'relation_query','每次只能查看 1 至 50 条声明。');
 await p.flush(3);
 assert.match(p.node('relation-claims-status').textContent,/每次只能查看 1 至 50 条声明/);
 assert.equal(p.text(itemsBox(p)),
  '还没有读取人际关系声明。点“读取关系声明”开始；本页只读取已有声明，不会新增关系。');
 assert.equal(p.text(detailBox(p)),'先在上方选择一条声明，这里会显示它的状态、证据、来源与依赖影响。');
 assert.equal(p.text(historyBox(p)),'');
 assert.equal(p.node('relation-claims-preview-area').hidden,true);
 // The page is idle again: the same button retries and a fresh claim starts clean.
 p.node('relation-claims-load').onclick();
 await p.reply(p.latest(),listing([claimView()]));
 await p.flush(2);
 await open(p,0);
 assert.equal(p.textareas(detailBox(p))[0].value,'');
 assert.equal(p.count('relations.commit'),0);
});

test('the status filter is applied to the request before paging',async()=>{
 const p=await ready();
 p.node('relation-claims-filter').value='disputed';
 p.node('relation-claims-filter').onchange();
 const request=p.latest();
 assert.equal(request.operation,'relations.list');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {after:'',limit:20,status:'disputed'});
 await p.reply(request,listing([claimView({status:'disputed',status_label:'有争议'})],
  {status:'disputed'}));
 await p.flush(2);
 assert.match(p.text(itemsBox(p)),/有争议/);
});

test('detail shows evidence only for a matching pinned version and names the rest',async()=>{
 const items=[
  evidenceItem(),
  evidenceItem({resource:'event/e2',state:'changed',state_label:
   '当前版本已更换，不是当初引用的版本；本页不展示新正文',matched:false,
   current_version:'ev2',excerpt:null}),
  evidenceItem({resource:'memories/m1',kind:'memories',state:'missing',
   state_label:'引用的记录已不存在，本页不重建原文',matched:false,excerpt:null,
   pinned_version:'v1',current_version:null}),
  evidenceItem({resource:'relation/x',kind:'relation',state:'unsupported',
   state_label:'这一类型不提供文本摘录',matched:false,excerpt:null})];
 const p=await open(await ready(),0,detail({evidence:evidence({items})}));
 const text=p.text(detailBox(p));
 assert.match(text,/引用版本下的原文摘录：合成的消息原文/);
 assert.match(text,/当前版本已更换，不是当初引用的版本；本页不展示新正文/);
 assert.match(text,/引用的记录已不存在，本页不重建原文/);
 assert.match(text,/这一类型不提供文本摘录/);
 assert.match(text,/只有引用版本与当前一致时才会给出摘录，不用现在的正文冒充当时的证据/);
 // Exactly one item carried an excerpt: the changed/missing/unsupported ones do not.
 assert.equal(p.tags(detailBox(p),'p').filter(line=>
  line.textContent.includes('引用版本下的原文摘录')).length,1);
 // The pinned version and the current version are both named.
 assert.match(text,/引用时版本：ev1/);
 assert.match(text,/当前版本：ev2/);
 assert.deepEqual(p.markup,[]);
});

test('evidence and account labels are rendered as text, never as markup',async()=>{
 const hostile='<img src=x onerror=alert(1)>';
 const p=await open(await ready([claimView({relation:hostile,
  subject:account('actor-a',hostile),declarer:account('actor-a',hostile)})]),0,
  detail({claim:claimView({relation:hostile,declared_reason:'<b>odd</b>',
   subject:account('actor-a',hostile),object:account('actor-z')}),
   evidence:evidence({items:[evidenceItem({resource:hostile,excerpt:hostile,
    state_label:'<svg onload=alert(3)>'})]}),
   source:report({ok:false,issues:[{resource:'<i>s</i>',detail:hostile}],total_issues:1})}),
 [historyItem({reason:'<u>why</u>'})]);
 const list=p.text(itemsBox(p));
 assert.match(list,/<img src=x onerror=alert\(1\)> → 微信 · wxid_b/);
 assert.match(list,/<img src=x onerror=alert\(1\)> · 仅声明（尚未核对）/);
 const text=p.text(detailBox(p));
 assert.match(text,/引用版本下的原文摘录：<img src=x onerror=alert\(1\)>/);
 assert.match(text,/<b>odd<\/b>/);
 // The primary flow states the human problem; the resource id stays in diagnostics.
 assert.match(text,/来源问题：<img src=x onerror=alert\(1\)>/);
 assert.match(text,/来源：<i>s<\/i> — <img src=x onerror=alert\(1\)>/);
 assert.match(text,/技术诊断（仅供核对）/);
 assert.match(text,/未登记账号/);
 assert.match(p.text(historyBox(p)),/<u>why<\/u>/);
 assert.deepEqual(p.markup,[]);
});

test('a late detail reply cannot replace a newer selection',async()=>{
 const p=await ready([claimView(),claimView({id:'c2',cursor:'c2',relation:'同学'})]);
 const rows=p.buttons(itemsBox(p));
 const older=rows[0].onclick();
 const first=p.latest();
 const newer=rows[1].onclick();
 const second=p.latest();
 assert.notEqual(first.request_id,second.request_id);
 await p.reply(second,detail({claim:claimView({id:'c2',cursor:'c2',relation:'同学'})}));
 const history=p.latest();
 await p.reply(history,historyPage([historyItem()]));
 await newer;
 await p.flush(3);
 assert.match(p.text(detailBox(p)),/同学/);
 await p.reply(first,detail({claim:claimView({relation:'旧声明'})}));
 await older;
 await p.flush(3);
 assert.match(p.text(detailBox(p)),/同学/);
 assert.equal(p.text(detailBox(p)).includes('旧声明'),false);
});

test('an unchanged status and an over-long reason are refused before any request',async()=>{
 const p=await open(await ready(),0);
 const before=p.requests.length;
 // The current status is not offered at all; forcing it is still refused locally.
 statusSelect(p).value='claimed';
 statusSelect(p).onchange();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.flush(2);
 assert.equal(p.requests.length,before);
 assert.match(p.text(detailBox(p)),/新状态与当前状态相同，没有需要提交的更改；本页不会为同状态增加版本/);
 // The reason is bounded to 1000 characters, counted before submitting.
 statusSelect(p).value='confirmed';
 statusSelect(p).onchange();
 p.textareas(detailBox(p))[0].value='说'.repeat(1001);
 p.textareas(detailBox(p))[0].oninput();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.flush(2);
 assert.equal(p.requests.length,before);
 assert.match(p.text(detailBox(p)),/更正说明最多 1000 字/);
 // A valid form finally asks the server, with the chosen status and reason.
 p.textareas(detailBox(p))[0].value='合成说明';
 p.textareas(detailBox(p))[0].oninput();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.flush(2);
 assert.equal(p.requests.length,before+1);
 const request=p.latest();
 assert.equal(request.operation,'relations.preview');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {identifier:'c1',status:'confirmed',reason:'合成说明'});
});

test('an inconsistent claim/valid pair blocks the form and never submits silently',async()=>{
 const p=await open(await ready(),0,detail({
  check:{consistent:false,anomaly:'status_valid_mismatch',
   detail:'这条声明的状态与它的权限有效标记不一致。'},
  policy:policyView({valid:false})}));
 const text=p.text(detailBox(p));
 assert.match(text,/检查异常：这条声明的状态与它的权限有效标记不一致/);
 assert.match(text,/本页不会默默把它改回有效，也不会提交任何更改/);
 const run=p.buttons(detailBox(p),'预览这次状态更正')[0];
 assert.equal(run.disabled,true);
 const before=p.requests.length;
 run.onclick();
 await p.flush(2);
 assert.equal(p.requests.length,before);
 assert.equal(p.count('relations.commit'),0);
});

test('restoring a revoked claim says so and never claims sources become readable',async()=>{
 const revoked=claimView({status:'revoked',status_label:'已撤销',queryable:false,
  restorable:true});
 const p=await open(await ready([revoked]),0,detail({claim:revoked,
  policy:policyView({valid:false}),
  source:report({ok:false,issues:[{resource:'event/e1',reason:'source_changed',
   detail:'引用的来源版本已变化'}],total_issues:1}),
  readable:readable({allowed:false,
   note:'按来源与有效状态核对：声明人自己的回复现在也不能把它作为依据。'})}));
 assert.match(p.text(detailBox(p)),/改回“已确认”或“有争议”就是恢复此声明/);
 statusSelect(p).value='confirmed';
 statusSelect(p).onchange();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 const request=p.latest();
 assert.equal(request.operation,'relations.preview');
 await p.reply(request,previewOf({status:'confirmed',claim:revoked,restored:true,
  before:{status:'revoked',status_label:'已撤销',version:'v1',policy_version:'p1',
   valid:false,queryable:false},
  after:{status:'confirmed',status_label:'已确认',valid:true,queryable:true,restores:true},
  source:report({ok:false,issues:[{resource:'event/e1',reason:'source_changed',
   detail:'引用的来源版本已变化'}],total_issues:1}),
  notice:'确认后恢复此声明（已撤销 → 已确认）：已失效或已更换的来源不会因此重新可读，'+
   '来源版本不会被重新固定，旧依赖版本也不会自动恢复。'}));
 await p.flush(3);
 const preview=p.text(previewBox(p));
 assert.match(preview,/这一次是恢复此声明/);
 assert.match(preview,/来源不被重新固定，旧依赖版本不自动恢复/);
 assert.match(preview,/确认后恢复此声明/);
 assert.match(preview,/已失效或已更换的来源不会因此重新可读/);
 assert.match(preview,/来源问题：引用的来源版本已变化/);
 assert.match(preview,/来源：event\/e1 — source_changed/);
 assert.match(preview,/保持不变：正文、主体、对象、声明人、原证据、归属账号与到期时间/);
 // Nothing here may promise the claim becomes readable again.
 assert.equal(preview.includes('重新可读的来源已经恢复'),false);
 assert.equal(p.count('relations.commit'),0);
});

test('confirm submits once, then reports the status change apart from source readability',async()=>{
 const p=await open(await ready(),0);
 statusSelect(p).value='confirmed';
 statusSelect(p).onchange();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.reply(p.latest(),previewOf());
 await p.flush(3);
 const previewId=p.latest().request_id;
 const confirm=p.buttons(previewBox(p),'确认提交')[0];
 confirm.onclick();
 confirm.onclick();
 await p.flush(2);
 assert.equal(p.count('relations.commit'),1);
 const commit=p.latest();
 assert.equal(commit.operation,'relations.commit');
 assert.deepEqual(JSON.parse(JSON.stringify(commit.arguments)),{token:'tok-1'});
 assert.notEqual(commit.request_id,previewId);
 await p.reply(commit,committed());
 await p.flush(4);
 const status=p.node('relation-claims-status').textContent;
 assert.match(status,/状态更改成功：仅声明（尚未核对） → 已确认/);
 assert.match(status,/按来源与有效状态核对：声明人自己的回复仍可以把它作为依据/);
 assert.match(status,/账号关联、回复授权与其他资料权限未改变/);
 assert.equal(p.node('relation-claims-preview-area').hidden,true);
 // The claim is re-read, so the page shows what the server really has now.
 const reread=p.latest();
 assert.equal(reread.operation,'relations.detail');
 assert.deepEqual(JSON.parse(JSON.stringify(reread.arguments)),{identifier:'c1'});
 await p.reply(reread,detail({claim:claimView({status:'confirmed',status_label:'已确认',
  version:'v2'}),policy:policyView({version:'v2'})}));
 await p.reply(p.latest(),historyPage([historyItem()]));
 await p.flush(3);
 assert.match(p.text(detailBox(p)),/已确认/);
 assert.deepEqual(p.markup,[]);
});

test('a commit whose sources stay unreadable reports success and the denial separately',async()=>{
 const p=await open(await ready(),0);
 statusSelect(p).value='confirmed';
 statusSelect(p).onchange();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.reply(p.latest(),previewOf());
 await p.flush(3);
 p.buttons(previewBox(p),'确认提交')[0].onclick();
 await p.reply(p.latest(),committed({usable:false,
  readable:readable({allowed:false,
   note:'按来源与有效状态核对：声明人自己的回复现在也不能把它作为依据。'}),
  readable_notice:'按来源与有效状态核对：声明人自己的回复现在也不能把它作为依据。'}));
 await p.flush(4);
 const status=p.node('relation-claims-status').textContent;
 assert.match(status,/状态更改成功/);
 assert.match(status,/不能把它作为依据/);
 // The status change is not reported as a failure.
 assert.equal(status.includes('提交没有完成'),false);
 assert.equal(status.includes('状态更改失败'),false);
});

test('a commit timeout reports an unknown outcome, never retries and re-reads the claim',async()=>{
 const p=await open(await ready(),0);
 statusSelect(p).value='revoked';
 statusSelect(p).onchange();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.reply(p.latest(),previewOf({status:'revoked'}));
 await p.flush(3);
 p.buttons(previewBox(p),'确认提交')[0].onclick();
 await p.flush(2);
 assert.equal(p.latest().operation,'relations.commit');
 p.fireTimers();
 await p.flush(4);
 const status=p.node('relation-claims-status').textContent;
 assert.match(status,/提交超时/);
 assert.match(status,/写入结果未知/);
 assert.match(status,/没有自动重试/);
 assert.equal(p.count('relations.commit'),1);
 const reread=p.latest();
 assert.equal(reread.operation,'relations.detail');
 assert.equal(p.node('relation-claims-preview-area').hidden,true);
 await p.reply(reread,detail());
 await p.reply(p.latest(),historyPage([historyItem()]));
 await p.flush(3);
 assert.equal(p.count('relations.commit'),1);
 assert.equal(p.buttons(detailBox(p),'预览这次状态更正')[0].disabled,false);
});

test('an expired preview marks the page stale and only a fresh read clears it',async()=>{
 const p=await open(await ready(),0);
 statusSelect(p).value='disputed';
 statusSelect(p).onchange();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.reply(p.latest(),previewOf({status:'disputed'}));
 await p.flush(3);
 p.buttons(previewBox(p),'确认提交')[0].onclick();
 await p.fail(p.latest(),'relation_preview_expired','预览已过期或管理会话已改变，请重新预览。');
 await p.flush(4);
 const status=p.node('relation-claims-status').textContent;
 assert.match(status,/预览已过期或管理会话已改变/);
 assert.match(status,/不会自动重新提交/);
 assert.equal(p.count('relations.commit'),1);
 const reread=p.latest();
 assert.equal(reread.operation,'relations.detail');
 await p.reply(reread,detail());
 await p.reply(p.latest(),historyPage([historyItem()]));
 await p.flush(3);
 assert.equal(p.buttons(detailBox(p),'预览这次状态更正')[0].disabled,false);
 assert.equal(p.node('relation-claims-preview-area').hidden,true);
});

test('cancel writes nothing, sends the token once and a second click is a no-op',async()=>{
 const p=await open(await ready(),0);
 statusSelect(p).value='confirmed';
 statusSelect(p).onchange();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.reply(p.latest(),previewOf());
 await p.flush(3);
 const cancel=p.buttons(previewBox(p),'取消这次预览')[0];
 cancel.onclick();
 cancel.onclick();
 await p.flush(2);
 assert.equal(p.count('relations.cancel'),1);
 const sent=p.requests.filter(entry=>entry.operation==='relations.cancel')[0];
 assert.deepEqual(JSON.parse(JSON.stringify(sent.arguments)),{token:'tok-1'});
 assert.equal(p.count('relations.commit'),0);
 assert.equal(p.node('relation-claims-preview-area').hidden,true);
 assert.match(p.node('relation-claims-status').textContent,/已取消这次预览，没有写入任何更改/);
 await p.reply(sent,{cancelled:true,written:false,identifier:'c1'});
 await p.flush(2);
 assert.equal(p.node('relation-claims-preview-area').hidden,true);
});

test('history pages append, keep their order and say where the record starts',async()=>{
 const first=[historyItem()];
 const p=await ready();
 p.buttons(itemsBox(p))[0].onclick();
 await p.reply(p.latest(),detail());
 const read=p.latest();
 assert.equal(read.operation,'relations.history');
 assert.deepEqual(JSON.parse(JSON.stringify(read.arguments)),
  {identifier:'c1',after:'',limit:20});
 await p.reply(read,historyPage(first,'000000000002'));
 await p.flush(3);
 let text=p.text(historyBox(p));
 assert.match(text,/这里共记录了 1 次状态更正/);
 assert.match(text,/本机 · 管理员：仅声明（尚未核对） → 已确认/);
 assert.match(text,/更正说明：合成说明/);
 assert.match(text,/历史按写入顺序列出，每条不可变，不会自动删除/);
 assert.match(text,/不包含本功能开始记录之前的裁决/);
 // The next page is read with the cursor the server returned and appended, not replaced.
 const more=p.buttons(historyBox(p),'读取更多变更历史')[0];
 assert.equal(more.disabled,false);
 more.onclick();
 const page2=p.latest();
 assert.equal(page2.operation,'relations.history');
 assert.deepEqual(JSON.parse(JSON.stringify(page2.arguments)),
  {identifier:'c1',after:'000000000002',limit:20});
 await p.reply(page2,historyPage([historyItem({cursor:'000000000002',sequence:2,at:1800000100,
  before:{status:'confirmed',status_label:'已确认',version:'v2',policy_version:'v2',valid:true},
  after:{status:'disputed',status_label:'有争议',version:'v3',policy_version:'v3',valid:true}})]));
 await p.flush(3);
 text=p.text(historyBox(p));
 assert.match(text,/仅声明（尚未核对） → 已确认/);
 assert.match(text,/已确认 → 有争议/);
 // A restored decision is named as one.
 p.buttons(historyBox(p),'重新读取变更历史')[0].onclick();
 const reload=p.latest();
 assert.equal(reload.operation,'relations.history');
 assert.deepEqual(JSON.parse(JSON.stringify(reload.arguments)),
  {identifier:'c1',after:'',limit:20});
 await p.reply(reload,historyPage([historyItem({restored:true,reason:'恢复说明',
  before:{status:'revoked',status_label:'已撤销',version:'v1',policy_version:'p1',valid:false},
  after:{status:'confirmed',status_label:'已确认',version:'v2',policy_version:'v2',valid:true}})]));
 await p.flush(3);
 text=p.text(historyBox(p));
 assert.match(text,/已撤销 → 已确认/);
 assert.match(text,/这一次记录恢复了已撤销的声明/);
 assert.match(text,/更正说明：恢复说明/);
 assert.equal(p.buttons(historyBox(p),'读取更多变更历史')[0].disabled,true);
});

test('a claim whose decisions predate this feature says so instead of inventing them',async()=>{
 const confirmed=claimView({status:'confirmed',status_label:'已确认'});
 const p=await open(await ready([confirmed]),0,detail({claim:confirmed,
  history_available:false,history:historyView({available:false,count:0,complete:false,
   note:'这条声明当前的“已确认”发生在本次管理功能开始记录之前，因此早先的裁决没有记录；'+
    '本页只从本次管理开始有记录，不虚构以前的裁决。'}),
  policy:policyView()}),[]);
 const text=p.text(historyBox(p));
 assert.match(text,/本次管理功能开始记录之前/);
 assert.match(text,/不虚构以前的裁决/);
 assert.match(text,/这条声明当前没有本页记录的裁决/);
});

test('a later recorded history states the version that recorded history started from',async()=>{
 const confirmed=claimView({status:'confirmed',status_label:'已确认',version:'v3'});
 const p=await open(await ready([confirmed]),0,detail({claim:confirmed,
  history:historyView({count:2,complete:false,current:true,
   note:'这里共记录了 2 次状态更正。最早一次记录的变更前状态不是“仅声明”，'+
    '说明更早的裁决发生在本次管理功能开始记录之前；本页不虚构那些裁决。'})}));
 const text=p.text(historyBox(p));
 assert.match(text,/最早一次记录的变更前状态不是“仅声明”/);
 assert.match(text,/不虚构那些裁决/);
});

test('a late history reply cannot paint the history of another claim',async()=>{
 const p=await ready([claimView(),claimView({id:'c2',cursor:'c2',relation:'同学'})]);
 const rows=p.buttons(itemsBox(p));
 rows[0].onclick();
 await p.reply(p.latest(),detail());
 const late=p.latest();
 assert.equal(late.operation,'relations.history');
 // Another claim is selected and read while the first history read is pending.
 rows[1].onclick();
 await p.reply(p.latest(),detail({claim:claimView({id:'c2',cursor:'c2',relation:'同学'})}));
 await p.reply(p.latest(),historyPage([historyItem({reason:'新的说明'})]));
 await p.flush(3);
 assert.match(p.text(historyBox(p)),/新的说明/);
 await p.reply(late,historyPage([historyItem({reason:'旧历史'})]));
 await p.flush(3);
 assert.match(p.text(historyBox(p)),/新的说明/);
 assert.equal(p.text(historyBox(p)).includes('旧历史'),false);
});

test('a failed history read leaves no stale history and can be retried',async()=>{
 const p=await ready();
 p.buttons(itemsBox(p))[0].onclick();
 await p.reply(p.latest(),detail());
 const read=p.latest();
 assert.equal(read.operation,'relations.history');
 await p.fail(read,'relation_history','历史分页位置无效，请重新读取。');
 await p.flush(3);
 const text=p.text(historyBox(p));
 assert.match(text,/历史分页位置无效，请重新读取/);
 assert.match(text,/可以点“读取变更历史”重试/);
 const retry=p.buttons(historyBox(p),'重新读取变更历史')[0];
 assert.equal(retry.disabled,false);
 retry.onclick();
 const again=p.latest();
 assert.equal(again.operation,'relations.history');
 await p.reply(again,historyPage([historyItem()]));
 await p.flush(3);
 assert.match(p.text(historyBox(p)),/本机 · 管理员/);
 assert.equal(p.text(historyBox(p)).includes('历史分页位置无效'),false);
});

test('paging the claim list appends and is disabled without a cursor',async()=>{
 const p=page();
 p.state(true,true);
 const first=p.latest();
 await p.reply(first,listing([claimView()],{next:'c1'}));
 await p.flush(2);
 assert.equal(p.node('relation-claims-next').disabled,false);
 p.node('relation-claims-next').onclick();
 const request=p.latest();
 assert.equal(request.operation,'relations.list');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),
  {after:'c1',limit:20,status:'all'});
 await p.reply(request,listing([claimView({id:'c2',cursor:'c2',relation:'同学'})],
  {after:'c1',next:null}));
 await p.flush(2);
 const text=p.text(itemsBox(p));
 assert.match(text,/同事/);
 assert.match(text,/同学/);
 assert.equal(p.node('relation-claims-next').disabled,true);
});

test('locking clears a typed draft and an open preview and late replies cannot return',async()=>{
 const p=await open(await ready(),0);
 const reason=p.textareas(detailBox(p))[0];
 reason.value='合成说明';
 reason.oninput();
 statusSelect(p).value='confirmed';
 statusSelect(p).onchange();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 const early=p.latest();
 await p.reply(early,previewOf());
 await p.flush(3);
 assert.equal(p.node('relation-claims-preview-area').hidden,false);
 p.state(true,false);
 assert.equal(p.text(detailBox(p)),'');
 assert.equal(p.text(historyBox(p)),'');
 assert.equal(p.node('relation-claims-preview-area').hidden,true);
 await p.reply(early,previewOf());
 await p.flush(2);
 assert.equal(p.node('relation-claims-preview-area').hidden,true);
 // Unlocking starts from scratch: no typed reason and no preview come back.
 p.state(true,true);
 await p.reply(p.latest(),listing([claimView()]));
 await p.flush(2);
 await open(p,0);
 assert.equal(p.textareas(detailBox(p))[0].value,'');
 assert.equal(p.node('relation-claims-preview-area').hidden,true);
});

test('a late locked rejection from a cancel cannot lock a reopened page',async()=>{
 const p=await open(await ready(),0);
 statusSelect(p).value='confirmed';
 statusSelect(p).onchange();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.reply(p.latest(),previewOf());
 await p.flush(3);
 p.buttons(previewBox(p),'取消这次预览')[0].onclick();
 const cancellation=p.latest();
 assert.equal(cancellation.operation,'relations.cancel');
 await p.flush(2);
 // The page is locked and unlocked again while the cancel is still on its way.
 p.state(true,false);
 await p.flush(2);
 p.state(true,true);
 await p.reply(p.latest(),listing([claimView()]));
 await p.flush(2);
 assert.equal(p.node('relation-claims-load').disabled,false);
 // That rejection belongs to the page as it was, not to the reopened one.
 await p.fail(cancellation,'desktop_locked','请先解锁栖伴管理功能');
 await p.flush(3);
 assert.equal(p.node('relation-claims-load').disabled,false);
 assert.match(p.node('relation-claims-status').textContent,/已读取 1 条声明/);
 assert.equal(p.node('relation-claims-panel').hidden,false);
});

test('a fresh read retires the old preview and draft even for the same claim',async()=>{
 const p=await open(await ready(),0);
 statusSelect(p).value='confirmed';
 statusSelect(p).onchange();
 const reason=p.textareas(detailBox(p))[0];
 reason.value='旧草稿';
 reason.oninput();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.reply(p.latest(),previewOf());
 await p.flush(3);
 assert.equal(p.node('relation-claims-preview-area').hidden,false);
 // The filter re-reads page one, and the same claim is still listed.
 p.node('relation-claims-filter').value='all';
 p.node('relation-claims-filter').onchange();
 const reload=p.latest();
 assert.equal(reload.operation,'relations.list');
 // The old preview is already gone, before the answer arrives.
 assert.equal(p.node('relation-claims-preview-area').hidden,true);
 await p.reply(reload,listing([claimView()]));
 const detailRequest=p.latest();
 assert.equal(detailRequest.operation,'relations.detail');
 await p.reply(detailRequest,detail());
 await p.reply(p.latest(),historyPage([historyItem()]));
 await p.flush(3);
 // Neither the typed reason nor an operable preview survives the re-read.
 assert.equal(p.textareas(detailBox(p))[0].value,'');
 assert.equal(p.node('relation-claims-preview-area').hidden,true);
 assert.equal(p.count('relations.commit'),0);
 assert.equal(p.count('relations.cancel'),0);
});

test('nothing can navigate or re-read while a commit is in flight',async()=>{
 const p=await open(await ready([claimView(),claimView({id:'c2',cursor:'c2',relation:'同学'})]),0);
 statusSelect(p).value='confirmed';
 statusSelect(p).onchange();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.reply(p.latest(),previewOf());
 await p.flush(3);
 p.buttons(previewBox(p),'确认提交')[0].onclick();
 await p.flush(2);
 const commit=p.latest();
 assert.equal(commit.operation,'relations.commit');
 assert.equal(p.node('relation-claims-load').disabled,true);
 assert.equal(p.node('relation-claims-filter').disabled,true);
 assert.equal(p.node('relation-claims-next').disabled,true);
 const before=p.requests.length;
 // Forced clicks during the write must not start another read or another choice.
 p.node('relation-claims-load').onclick();
 p.node('relation-claims-filter').onchange();
 p.node('relation-claims-next').onclick();
 p.buttons(itemsBox(p))[1].onclick();
 await p.flush(3);
 assert.equal(p.requests.length,before);
 assert.equal(p.count('relations.commit'),1);
 // After the answer the page re-reads the claim and is usable again.
 await p.reply(commit,committed());
 await p.flush(4);
 const reread=p.latest();
 assert.equal(reread.operation,'relations.detail');
 await p.reply(reread,detail());
 await p.reply(p.latest(),historyPage([historyItem()]));
 await p.flush(3);
 assert.equal(p.node('relation-claims-load').disabled,false);
 assert.equal(p.node('relation-claims-filter').disabled,false);
 assert.equal(p.count('relations.commit'),1);
});

for(const filter of ['all','claimed'])test('committed status updates the list with filter '+filter,async()=>{
 const p=await open(await ready(),0);
 p.node('relation-claims-filter').value=filter;
 statusSelect(p).value='confirmed';
 statusSelect(p).onchange();
 p.buttons(detailBox(p),'预览这次状态更正')[0].onclick();
 await p.reply(p.latest(),previewOf());
 await p.flush(3);
 p.buttons(previewBox(p),'确认提交')[0].onclick();
 await p.reply(p.latest(),committed());
 await p.flush(3);
 assert.equal(p.latest().operation,'relations.detail');
 await p.reply(p.latest(),detail({claim:claimView({status:'confirmed',version:'v2'})}));
 await p.reply(p.latest(),historyPage([historyItem()]));
 await p.flush(3);
 assert.match(p.text(detailBox(p)),/同事 · 已确认/);
 assert.doesNotMatch(p.text(itemsBox(p)),/仅声明/);
 if(filter==='all')assert.match(p.text(itemsBox(p)),/同事 · 已确认/);
 else assert.equal(p.buttons(itemsBox(p)).length,0);
 assert.equal(p.count('relations.commit'),1);
});
