// Reply trace page tests use only synthetic records; nothing here reaches a database.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');

function page(){
 const nodes=new Map(),events={},requests=[],listeners=[],timers=new Map(),markup=[];
 const element=tag=>{
  const node={tagName:tag,textContent:'',hidden:false,disabled:false,value:'',className:'',
   type:'',min:'',max:'',step:'',attributes:{},children:[],
   append(...kids){for(const kid of kids)this.children.push(kid);},
   replaceChildren(...kids){this.children=kids;this.value='';},
   setAttribute(name,value){this.attributes[name]=value;},
   removeAttribute(name){delete this.attributes[name];}};
  // Assigning raw markup would bypass escaping; record it so a test can refuse it.
  for(const name of ['innerHTML','outerHTML','insertAdjacentHTML']){
   Object.defineProperty(node,name,{set(value){markup.push({tag,name,value});},get(){return '';}});
  }
  return node;
 };
 const node=id=>{if(!nodes.has(id))nodes.set(id,element('div'));return nodes.get(id);};
 let sequence=0;
 const webview={addEventListener(_,fn){listeners.push(fn);},postMessage(request){requests.push(request);}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/reply_trace.js'),'utf8'),{
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
 function tags(target,name){
  const found=[];walk(target,n=>{if(n.tagName===name)found.push(n);});return found;
 }
 return {node,requests,markup,state,reply,fail,settle,flush,fireTimers,texts,buttons,tags};
}

const STARTED='2026-11-08T04:00:00+00:00';

function row(overrides={}){
 return {id:STARTED+':'+'a'.repeat(32),event_id:'event-synthetic',platform:'qq',generation:1,
  started:STARTED,status:'completed',duration_ms:1234,...overrides};
}

function trace(overrides={}){
 return {...row(),steps:[
  {stage:'context',memory_records:2,summary_present:false},
  {stage:'reply.generate',status:'success',duration_ms:190,input_tokens:321},
  {stage:'custom_stage',note:'额外信息'}],
  sources:[{resource:'memories/owned',version:'v1',version_current:true},
   {resource:'memories/rotated',version:'r1',version_current:false}],
  hidden_sources:1,sources_limited:true,source_count:3,
  meaning:'程序处理记录；不保存提示词、原文或模型内部推理。来源按当前权限再次过滤。',...overrides};
}

function listing(items,overrides={}){
 return {items,limit:30,count:items.length,recent_only:true,
  meaning:'程序处理记录：只包含处理阶段、耗时、结果和来源版本，不含提示词原文、聊天原文或模型内部推理；不提供导出、删除或保留期修改。',
  ...overrides};
}

function latest(p){return p.requests.at(-1);}

async function loaded(items){
 const p=page();
 p.state(true,true);
 assert.equal(latest(p).operation,'reply_trace.list');
 await p.reply(latest(p),listing(items||[row(),row({id:STARTED+':'+'b'.repeat(32),platform:'wechat',
  status:'failed',duration_ms:88})]));
 await p.flush(2);
 return p;
}

async function opened(items,record){
 const p=await loaded(items);
 const first=p.node('reply-trace-list').children[1];
 assert.equal(first.tagName,'button');
 const job=first.onclick();
 const request=latest(p);
 assert.equal(request.operation,'reply_trace.detail');
 await p.reply(request,record||trace());
 await job;
 return {p,request};
}

test('an empty history is stated plainly and the button stays usable',async()=>{
 const p=page();
 p.state(true,true);
 const request=latest(p);
 assert.equal(request.operation,'reply_trace.list');
 assert.deepEqual(JSON.parse(JSON.stringify(request.arguments)),{limit:30});
 await p.reply(request,listing([]));
 await p.flush(2);
 const text=p.texts(p.node('reply-trace-list')).join('|');
 assert.match(text,/本机还没有回复处理记录/);
 assert.match(text,/超过保留期后会被自动清理/);
 assert.match(p.node('reply-trace-status').textContent,/已读取最近 0 条回复处理记录/);
 assert.equal(p.node('reply-trace-load').disabled,false);
 // The area follows the management panel, including when nothing else changed.
 p.node('management-panel').hidden=true;
 p.state(true,true);
 assert.equal(p.node('reply-trace-panel').hidden,true);
});

test('a failed read is reported as a failure and the same button retries it',async()=>{
 const p=page();
 p.state(true,true);
 await p.fail(latest(p),'reply_trace_invalid','最近记录数量必须是 1 至 100 的整数。');
 await p.flush(2);
 assert.match(p.node('reply-trace-status').textContent,/最近记录数量必须是 1 至 100 的整数/);
 assert.match(p.node('reply-trace-status').textContent,/重试/);
 // A failed read must not look like an empty history.
 assert.equal(p.texts(p.node('reply-trace-list')).join('|').includes('本机还没有回复处理记录'),false);
 assert.equal(p.node('reply-trace-load').disabled,false);
 const count=p.requests.length;
 p.node('reply-trace-load').onclick();
 await p.flush(2);
 assert.equal(p.requests.length,count+1);
 assert.equal(latest(p).operation,'reply_trace.list');
 await p.reply(latest(p),listing([row()]));
 await p.flush(2);
 assert.match(p.texts(p.node('reply-trace-list')).join('|'),/QQ · 完成 · 1234 毫秒/);
});

test('the recent count is checked before anything is read',async()=>{
 const p=await loaded();
 const count=p.requests.length;
 let sent=count;
 for(const value of ['0','101','','-3','30 条','3.5','9999999999']){
  p.node('reply-trace-limit').value=value;
  p.node('reply-trace-load').onclick();
  await p.flush(2);
  assert.equal(p.requests.length,sent,'sent a request for '+JSON.stringify(value));
  assert.match(p.node('reply-trace-status').textContent,/最近数量/);
 }
 for(const value of ['1','100']){
  p.node('reply-trace-limit').value=value;
  p.node('reply-trace-load').onclick();
  await p.flush(2);
  assert.equal(p.requests.length,++sent);
  assert.deepEqual(JSON.parse(JSON.stringify(latest(p).arguments)),{limit:Number(value)});
  // The input and button are disabled while the read is in flight.
  assert.equal(p.node('reply-trace-limit').disabled,true);
  assert.equal(p.node('reply-trace-load').disabled,true);
  await p.reply(latest(p),listing([row()]));
  await p.flush(2);
  assert.equal(p.node('reply-trace-load').disabled,false);
 }
});

test('a record shows platform, times, stages and source version state',async()=>{
 const {p}=await opened();
 const text=p.texts(p.node('reply-trace-detail')).join('|');
 assert.match(text,/记录状态：完成/);
 assert.match(text,/平台：QQ/);
 assert.match(text,/开始时间：2026\/11\/8 12:00:00|开始时间：2026\/11\/8 12:00:00/);
 assert.match(text,/结束时间：/);
 assert.match(text,/用时：1234 毫秒/);
 assert.match(text,/处理阶段（3 个）/);
 assert.match(text,/选择模型|组装上下文/);
 assert.match(text,/生成回复/);
 // An unknown stage name and an unknown metadata key stay readable as plain text.
 assert.match(text,/custom_stage/);
 assert.match(text,/note：额外信息/);
 assert.match(text,/可用记忆条数：2/);
 assert.match(text,/含对话摘要：否/);
 assert.match(text,/资源：memories\/owned；版本：v1；仍为当前版本/);
 assert.match(text,/资源：memories\/rotated；版本：r1；已不是当前版本/);
 assert.match(text,/另有 1 个来源按当前读权限不可读，已隐藏/);
 assert.match(text,/记录只保留了前 100 个（已截断）/);
 assert.match(text,/不提供打开原文的入口/);
 assert.match(text,/不保存提示词、原文或模型内部推理/);
 // Never raw markup: the page builds text nodes only.
 assert.deepEqual(p.markup,[]);
});

test('untrusted field values are rendered as text and never as markup',async()=>{
 const {p}=await opened([row()],trace({platform:'<img src=x onerror=alert(1)>',status:'<b>odd</b>',
  steps:[{stage:'<script>alert(2)</script>','"><i>key':'<u>value</u>'}],
  sources:[{resource:'<svg onload=alert(3)>',version:'<em>v</em>',version_current:true}]}));
 const text=p.texts(p.node('reply-trace-detail')).join('|');
 assert.match(text,/平台：<img src=x onerror=alert\(1\)>/);
 assert.match(text,/未识别的状态（<b>odd<\/b>）/);
 assert.match(text,/资源：<svg onload=alert\(3\)>；版本：<em>v<\/em>；仍为当前版本/);
 assert.match(text,/"><i>key：<u>value<\/u>/);
 assert.match(text,/<script>alert\(2\)<\/script>/);
 assert.deepEqual(p.markup,[]);
});

test('unknown states stay readable and an expired record never looks loaded',async()=>{
 const p=await loaded([row({status:'paused_by_operator',duration_ms:null}),
  row({id:STARTED+':'+'c'.repeat(32),status:'running',duration_ms:null}),row()]);
 const buttons=p.buttons(p.node('reply-trace-list'));
 assert.match(buttons[0].textContent,/未识别的状态（paused_by_operator）/);
 assert.match(buttons[0].textContent,/用时未知/);
 assert.match(buttons[1].textContent,/处理中/);
 const job=buttons[2].onclick();
 await p.fail(latest(p),'not_found','回复追踪不存在或已过期');
 await job;
 const text=p.texts(p.node('reply-trace-detail')).join('|');
 assert.match(text,/回复追踪不存在或已过期/);
 assert.equal(text.includes('记录状态：完成'),false);
 assert.equal(p.texts(p.node('reply-trace-detail')).some(line=>line.startsWith('记录标识：')),false);
 // The row stays clickable so the read can be retried.
 assert.equal(p.buttons(p.node('reply-trace-list'))[2].disabled,false);
});

test('a running record says so instead of inventing an end time',async()=>{
 const {p}=await opened([row()],trace({status:'running',duration_ms:null}));
 const text=p.texts(p.node('reply-trace-detail')).join('|');
 assert.match(text,/记录状态：处理中/);
 assert.match(text,/结束时间：仍在处理中，还没有结束时间/);
 assert.match(text,/用时：仍在处理中/);
});

test('a late detail reply cannot replace a newer selection',async()=>{
 const p=await loaded();
 const rows=p.buttons(p.node('reply-trace-list'));
 const older=rows[0].onclick();
 const first=latest(p);
 const newer=rows[1].onclick();
 const second=latest(p);
 assert.notEqual(first.request_id,second.request_id);
 await p.reply(second,trace({id:'b'.repeat(32),platform:'wechat'}));
 await newer;
 assert.match(p.texts(p.node('reply-trace-detail')).join('|'),/平台：微信/);
 await p.reply(first,trace({id:'a'.repeat(32),platform:'qq'}));
 await older;
 const text=p.texts(p.node('reply-trace-detail')).join('|');
 assert.match(text,/平台：微信/);
 assert.equal(text.includes('平台：QQ'),false);
});

test('a reloaded list that drops the selected record cannot be repainted by its pending detail',async()=>{
 const p=await loaded();
 // 1. A detail read for the first row is requested and stays pending.
 const older=p.buttons(p.node('reply-trace-list'))[0].onclick();
 const detailRequest=latest(p);
 assert.equal(detailRequest.operation,'reply_trace.detail');
 // 2. While it is pending the list is read again and no longer contains that record.
 p.node('reply-trace-load').onclick();
 await p.flush(2);
 const listRequest=latest(p);
 assert.equal(listRequest.operation,'reply_trace.list');
 await p.reply(listRequest,listing([row({id:STARTED+':'+'d'.repeat(32)})]));
 await p.flush(2);
 // The removed record and its loading text are gone; the list itself loaded fine.
 assert.deepEqual(p.texts(p.node('reply-trace-detail')),[]);
 assert.match(p.node('reply-trace-status').textContent,/已读取最近 1 条回复处理记录/);
 assert.equal(p.texts(p.node('reply-trace-list')).join('|').includes('还没有回复处理记录'),false);
 // 3. The older detail reply arrives last and must not paint the dropped record back.
 await p.reply(detailRequest,trace({id:'a'.repeat(32),platform:'wechat'}));
 await older;
 assert.deepEqual(p.texts(p.node('reply-trace-detail')),[]);
 assert.equal(p.node('reply-trace-list').children.length,3);
});

test('a failed list reload clears a pending detail instead of leaving it loading',async()=>{
 const p=await loaded();
 const pendingRow=p.buttons(p.node('reply-trace-list'))[0].onclick();
 const detailRequest=latest(p);
 await p.flush(2);
 assert.match(p.texts(p.node('reply-trace-detail')).join('|'),/正在读取这条记录/);
 p.node('reply-trace-load').onclick();
 await p.flush(2);
 // 1. The reload fails, which clears the list.
 await p.fail(latest(p),'reply_trace_invalid','最近记录数量必须是 1 至 100 的整数。');
 await p.flush(2);
 // 2. No stale "正在读取" note survives the cleared list.
 assert.deepEqual(p.texts(p.node('reply-trace-detail')),[]);
 assert.match(p.node('reply-trace-status').textContent,/重试/);
 assert.equal(p.texts(p.node('reply-trace-list')).join('|').includes('还没有回复处理记录'),false);
 // 3. The detail reply arriving afterwards cannot repaint the cleared page.
 await p.reply(detailRequest,trace());
 await pendingRow;
 assert.deepEqual(p.texts(p.node('reply-trace-detail')),[]);
 assert.equal(p.node('reply-trace-load').disabled,false);
});

test('a pending detail still paints when the reloaded list keeps its record',async()=>{
 const p=await loaded();
 const job=p.buttons(p.node('reply-trace-list'))[0].onclick();
 const detailRequest=latest(p);
 p.node('reply-trace-load').onclick();
 await p.flush(2);
 // Same two records: the selection stays, so the read is still legitimate.
 await p.reply(latest(p),listing([row(),row({id:STARTED+':'+'b'.repeat(32),platform:'wechat',
  status:'failed',duration_ms:88})]));
 await p.flush(2);
 assert.match(p.texts(p.node('reply-trace-detail')).join('|'),/正在读取这条记录/);
 await p.reply(detailRequest,trace());
 await job;
 assert.match(p.texts(p.node('reply-trace-detail')).join('|'),/记录状态：完成/);
});

test('locking clears the page and delays no reply back onto it',async()=>{
 const {p,request}=await opened();
 assert.match(p.texts(p.node('reply-trace-detail')).join('|'),/记录状态：完成/);
 const late=p.buttons(p.node('reply-trace-list'))[0].onclick();
 const pending=latest(p);
 p.state(true,false);
 await p.flush(2);
 assert.equal(p.node('reply-trace-status').textContent,'');
 assert.deepEqual(p.texts(p.node('reply-trace-detail')),[]);
 assert.equal(p.texts(p.node('reply-trace-list')).join('|'),'最近记录|还没有读取最近记录。');
 await p.reply(request,trace());
 await late;
 await p.reply(pending,trace({platform:'wechat'}));
 await late;
 assert.deepEqual(p.texts(p.node('reply-trace-detail')),[]);
 assert.equal(p.node('reply-trace-list').children.length,2);
 // Unlocking reads again from scratch.
 p.state(true,true);
 await p.reply(latest(p),listing([row()]));
 await p.flush(2);
 assert.match(p.texts(p.node('reply-trace-list')).join('|'),/QQ · 完成/);
});

test('a read that never returns times out with a nearby retry',async()=>{
 const p=page();
 p.state(true,true);
 p.fireTimers();
 await p.flush(3);
 assert.match(p.node('reply-trace-status').textContent,/操作超时/);
 assert.match(p.node('reply-trace-status').textContent,/重试/);
 assert.equal(p.node('reply-trace-load').disabled,false);
 p.node('reply-trace-load').onclick();
 await p.flush(2);
 await p.reply(latest(p),listing([row()]));
 await p.flush(2);
 assert.match(p.texts(p.node('reply-trace-list')).join('|'),/QQ · 完成/);
});
