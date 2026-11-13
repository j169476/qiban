const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),events={},requests=[],listeners=[];
 const create=()=>({textContent:'',value:'',children:[],disabled:false,hidden:false,attrs:{},
  setAttribute(k,v){this.attrs[k]=v;},replaceChildren(){this.children=[];},append(...items){this.children.push(...items);}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create());return nodes.get(id);};let sequence=0;
 const webview={addEventListener(_,fn){listeners.push(fn);},postMessage(r){requests.push(r);}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/memories.js'),'utf8'),{
  document:{getElementById:node,createElement:create},window:{chrome:{webview},addEventListener(n,fn){events[n]=fn;}},
  crypto:{randomUUID:()=>String(++sequence)},setTimeout:()=>sequence,clearTimeout(){}
 });
 async function reply(r,value,ok=true){listeners.forEach(fn=>fn({data:{type:'management',request_id:r.request_id,response:ok?{ok:true,result:value}:{ok:false,message:value}}}));await new Promise(resolve=>setImmediate(resolve));}
 return {node,requests,reply,update:allowed=>events['qiban-management-state']({detail:{ready:true,locked:!allowed}})};
}
const item={version:'v1',data:{schema_version:1,id:'one',kind:'experience',content:'记忆中文🙂',tags:['旧标签'],confidence:'observed',locked:false,source:{type:'conversation',reference:'来源一',recorded_at:'2026-11-05T00:00:00Z'}},policy:{classification:'personal'}};
async function load(p){p.update(true);await p.reply(p.requests.at(-1),{records:[{id:'one'}],next:'one'});await p.reply(p.requests.at(-1),item);}
async function select(p){const work=p.node('memory-items').children[0].onclick();await p.reply(p.requests.at(-1),item);await work;}

const factItem={...item,data:{...item.data,id:'fact-one',fact:{attribute:'residence',value:'上海'}}};
const factPage={revision:'r1',total:2,counts:{active:0,disputed:2,historical:0,future:0},at:'2026-11-07T00:00:00Z',next:null,
 items:[{id:'fact-one',attribute:'residence',value:'上海',quote:'我住上海',valid_from:'2026-01-01',status:'disputed',source:{reference:'来源'}},
 {id:'fact-two',attribute:'residence',value:'杭州',quote:'我住杭州',valid_from:'2026-02-01',status:'disputed',source:{reference:'来源二'}}]};
async function documentPage(p,kind){
 await load(p);const data={...item.data,id:'same-id',locked:true,coverage:{kind:'web',complete:false,bvid:''},disagrees_with:['other'],memory_refs:[{id:'memory-ref',version:'a'.repeat(64)}]};
 delete data.kind;delete data.confidence;const doc={...item,data};
 p.node('memory-kind').value=kind;const work=p.node('memory-kind').onchange();assert.equal(p.requests.at(-1).arguments.kind,kind);
 await p.reply(p.requests.at(-1),{records:[{id:data.id}],next:null});await p.reply(p.requests.at(-1),doc);await work;
 const select=p.node('memory-items').children[0].onclick();await p.reply(p.requests.at(-1),doc);await select;return doc;
}
for(const kind of ['knowledge','diaries'])test('editing '+kind+' preserves locks metadata and blocks switching with a draft',async()=>{
 const p=page(),doc=await documentPage(p,kind);
 assert.equal(p.node('fact-area').hidden,true);assert.equal(p.node('memory-content').disabled,false);
 assert.match(p.node('memory-meta').textContent,/已锁定自动修改/);assert.match(p.node('memory-related').textContent,/关联记忆：memory-ref/);
 p.node('memory-content').value='修改正文🙂';p.node('memory-content').oninput();
 const count=p.requests.length;p.node('memory-kind').value='memories';p.node('memory-kind').onchange();
 assert.equal(p.requests.length,count);assert.equal(p.node('memory-kind').value,kind);
 let work=p.node('memory-save').onclick(),r=p.requests.at(-1);
 assert.equal(r.arguments.kind,kind);assert.equal(r.arguments.value.locked,true);
 for(const key of ['coverage','disagrees_with','memory_refs','source'])assert.equal(JSON.stringify(r.arguments.value[key]),JSON.stringify(doc.data[key]));
 await p.reply(r,'版本已变化',false);await work;assert.equal(p.node('memory-content').value,'修改正文🙂');
 work=p.node('memory-save').onclick();await p.reply(p.requests.at(-1),{version:'v2'});await work;
 assert.match(p.node('memory-notice').textContent,/已保存/);assert.match(p.node('memory-meta').textContent,/已锁定/);
 work=p.node('memory-delete-preview').onclick();assert.equal(p.requests.at(-1).arguments.kind,kind);await p.reply(p.requests.at(-1),{token:'document-delete',affected_count:1,effect:'保留历史'});await work;
 p.node('memory-delete-cancel').onclick();assert.match(p.node('memory-notice').textContent,/未改变/);
});
test('failed type load cannot leave old record actions under the new kind',async()=>{
 const p=page();await load(p);await select(p);p.node('memory-kind').value='knowledge';const work=p.node('memory-kind').onchange();
 await p.reply(p.requests.at(-1),'读取失败',false);await work;
 assert.equal(p.node('memory-editor').hidden,true);assert.equal(p.node('memory-items').children.length,0);
 assert.equal(p.node('memory-save').disabled,true);assert.equal(p.node('memory-delete-preview').disabled,true);
});
test('retiring a fact keeps an inspectable archive and disables old-value adoption',async()=>{
 const p=page();await loadFacts(p);
 assert.match(p.node('memory-delete-preview').textContent,/停用/);
 let work=p.node('memory-delete-preview').onclick();await p.reply(p.requests.at(-1),{token:'t',affected_count:2,effect:'保留历史'});await work;
 work=p.node('memory-delete-confirm').onclick();await p.reply(p.requests.at(-1),{retired:true});
 assert.equal(p.requests.at(-1).operation,'records.read');
 await p.reply(p.requests.at(-1),{...factItem,retirement:{at:'2026-11-07'}});await work;
 assert.match(p.node('memory-notice').textContent,/旧值不会恢复/);assert.equal(p.node('memory-delete-preview').disabled,true);
 assert.equal(p.node('fact-at').disabled,true);assert.equal(p.node('fact-refresh').disabled,false);
 work=p.node('fact-refresh').onclick();await p.reply(p.requests.at(-1),{...factPage,counts:{...factPage.counts,disputed:0,disabled:2},items:factPage.items.map(r=>({...r,status:'disabled'}))});await work;
 assert.match(p.node('fact-summary').textContent,/已停用 2/);assert.equal(p.node('fact-items').children[0].children[1].disabled,true);
 assert.equal(p.node('fact-confirm').disabled,true);
});
test('retention feedback distinguishes indexing collection and unverifiable sources',async()=>{
 const p=page();await load(p);
 for(const [snapshots,expected] of [[{status:'indexing',removed:0},/核对旧记录/],[{status:'checked',removed:3,unverified:2},/回收 3 条.*2 条来源关系尚未核实/]]){
  const work=p.node('fact-retention-read').onclick();await p.reply(p.requests.at(-1),{days:90,revision:'r1',last_cleanup:{examined:1,removed:0,referenced_or_active:1,snapshots}});await work;
  assert.match(p.node('fact-retention-notice').textContent,expected);
 }
});
test('retention setting requires reading current version and keeps failed edits',async()=>{
 const p=page();await load(p);
 assert.equal(p.node('fact-retention-save').disabled,true);
 let work=p.node('fact-retention-read').onclick();await p.reply(p.requests.at(-1),{days:90,revision:'r1',maintenance_error:'failed'});await work;
 assert.match(p.node('fact-retention-notice').textContent,/异常/);
 p.node('fact-retention-days').value='0';p.node('fact-retention-days').oninput();
 const n=p.requests.length;p.node('fact-retention-save').onclick();assert.equal(p.requests.length,n);
 p.node('fact-retention-days').value='120';p.node('fact-retention-days').oninput();
 work=p.node('fact-retention-save').onclick();assert.equal(p.requests.at(-1).arguments.expected,'r1');await p.reply(p.requests.at(-1),'版本已改变',false);await work;
 assert.equal(p.node('fact-retention-days').value,'120');assert.match(p.node('fact-retention-notice').textContent,/版本/);
 p.update(false);assert.equal(p.node('fact-retention-days').value,'');assert.equal(p.node('fact-retention-save').disabled,true);
});
async function loadFacts(p){
 await load(p);const work=p.node('memory-items').children[0].onclick();await p.reply(p.requests.at(-1),factItem);await work;
 const read=p.node('fact-refresh').onclick();await p.reply(p.requests.at(-1),factPage);await read;
}
test('fact confirmation selects explicit replacements and preserves history',async()=>{
 const p=page();await loadFacts(p);
 p.node('fact-items').children[0].children[1].onclick();
 const check=p.node('fact-items').children[1].children[2].children[0];check.checked=true;check.onchange();
 p.node('fact-reason').value='核实🙂';p.node('fact-reason').oninput();
 const work=p.node('fact-confirm').onclick(),r=p.requests.at(-1);
 assert.equal(r.operation,'facts.confirm');assert.equal(r.arguments.expected,'r1');
 assert.deepEqual(Array.from(r.arguments.supersedes),['fact-one','fact-two']);
 await p.reply(r,{id:'fact-new',history_preserved:true});await work;
 assert.match(p.node('fact-notice').textContent,/已追加/);assert.equal(p.node('fact-confirm').disabled,true);
});
test('historical view cannot confirm; locking rejects late fact details',async()=>{
 const p=page();await loadFacts(p);p.node('fact-at').value='2026-01-01T00:00:00Z';p.node('fact-at').oninput();
 let work=p.node('fact-refresh').onclick();await p.reply(p.requests.at(-1),factPage);await work;
 p.node('fact-items').children[0].children[1].onclick();assert.equal(p.node('fact-confirm').disabled,true);
 work=p.node('fact-refresh').onclick();const request=p.requests.at(-1);p.update(false);await p.reply(request,factPage);await work;
 assert.equal(p.node('fact-items').children.length,0);assert.equal(p.node('fact-area').hidden,true);
});
test('changed timeline discards cross-page choices; cancelling never writes',async()=>{
 const p=page();await loadFacts(p);p.node('fact-items').children[0].children[1].onclick();
 const n=p.requests.length;p.node('fact-clear').onclick();assert.equal(p.requests.length,n);
 let work=p.node('fact-refresh').onclick();await p.reply(p.requests.at(-1),{...factPage,next:'cursor'});await work;
 p.node('fact-items').children[0].children[1].onclick();
 work=p.node('fact-next').onclick();await p.reply(p.requests.at(-1),{...factPage,revision:'r2'});await work;
 assert.equal(p.node('fact-confirm').disabled,true);assert.match(p.node('fact-notice').textContent,/变化/);
});
test('manual correction preserves draft on failure, blocks generic editing, and labels provenance',async()=>{
 const p=page();await loadFacts(p);
 assert.equal(p.node('memory-content').disabled,true);assert.equal(p.node('memory-tags').disabled,true);
 p.node('fact-items').children[0].children[1].onclick();
 p.node('fact-value').value='北京';p.node('fact-value').oninput();p.node('fact-reason').value='核对后改错';p.node('fact-reason').oninput();
 assert.equal(p.node('fact-confirm').disabled,true);assert.equal(p.node('memory-refresh').disabled,true);
 let work=p.node('fact-correct').onclick(),r=p.requests.at(-1);assert.equal(r.operation,'facts.correct');assert.equal(r.arguments.value,'北京');
 await p.reply(r,'保存中断，请重新核对',false);await work;
 assert.equal(p.node('fact-value').value,'北京');assert.match(p.node('fact-notice').textContent,/保存中断/);
 work=p.node('fact-correct').onclick();await p.reply(p.requests.at(-1),{id:'fact-correction',history_preserved:true});await work;
 assert.match(p.node('fact-notice').textContent,/更正已保存/);assert.equal(p.node('fact-value').value,'');
 work=p.node('fact-refresh').onclick();await p.reply(p.requests.at(-1),{...factPage,items:[{...factPage.items[0],value:'北京',correction:{original_value:'上海',reason:'核对后改错'}}]});await work;
 assert.match(p.node('fact-items').children[0].children[0].textContent,/原始来源原话（更正前）/);
 assert.match(p.node('fact-items').children[0].children[0].textContent,/上海 → 北京/);
});
test('renders content as text, preserves source and fields in versioned save',async()=>{
 const p=page();await load(p);await select(p);
 p.node('memory-content').value='<img src=x> 新内容';p.node('memory-content').oninput();
 p.node('memory-tags').value='中文，数字123';
 const work=p.node('memory-save').onclick(),r=p.requests.at(-1);
 assert.equal(r.operation,'records.correct');assert.equal(r.arguments.expected,'v1');
 assert.equal(r.arguments.value.source.reference,'来源一');assert.equal(r.arguments.value.locked,false);
 assert.equal(JSON.stringify(r.arguments.value.tags),JSON.stringify(['中文','数字123']));
 await p.reply(r,{version:'v2'});await work;
 assert.match(p.node('memory-notice').textContent,/已保存/);
 assert.equal(p.node('memory-items').children[0].textContent,'<img src=x> 新内容');
 assert.equal(p.node('memory-save').disabled,true);
});
test('heartbeat and failed save retain draft; navigation waits for explicit discard',async()=>{
 const p=page();await load(p);await select(p);
 p.node('memory-content').value='未保存';p.node('memory-content').oninput();
 const count=p.requests.length;for(let i=0;i<5;i++)p.update(true);
 p.node('memory-next').onclick();assert.equal(p.requests.length,count);
 const work=p.node('memory-save').onclick();await p.reply(p.requests.at(-1),'资料已变化，请重新读取',false);await work;
 assert.equal(p.node('memory-content').value,'未保存');assert.equal(p.node('memory-refresh').disabled,true);
 p.node('memory-discard').onclick();assert.equal(p.node('memory-content').value,item.data.content);
 assert.equal(p.node('memory-refresh').disabled,false);
});
test('locking clears data and rejects late detail without issuing further requests',async()=>{
 const p=page();await load(p);const work=p.node('memory-items').children[0].onclick();
 const r=p.requests.at(-1),count=p.requests.length;p.update(false);await p.reply(r,item);await work;
 assert.equal(p.node('memory-content').value,'');assert.equal(p.node('memory-items').children.length,0);
 assert.equal(p.node('memory-source').textContent,'');assert.equal(p.requests.length,count);
});
test('delete requires preview, cancel changes nothing, expired preview reports failure',async()=>{
 const p=page();await load(p);await select(p);const count=p.requests.length;
 p.node('memory-delete-confirm').onclick();assert.equal(p.requests.length,count);
 let work=p.node('memory-delete-preview').onclick();await p.reply(p.requests.at(-1),{token:'first',affected_count:3,effect:'历史保留'});await work;
 p.node('memory-delete-cancel').onclick();assert.equal(p.requests.length,count+1);assert.equal(p.node('memory-content').value,item.data.content);
 work=p.node('memory-delete-preview').onclick();await p.reply(p.requests.at(-1),{token:'next',affected_count:3,effect:'历史保留'});await work;
 work=p.node('memory-delete-confirm').onclick();assert.equal(p.requests.at(-1).arguments.token,'next');
 await p.reply(p.requests.at(-1),'删除预览失效',false);await work;
 assert.match(p.node('memory-notice').textContent,/失效/);assert.equal(p.node('memory-content').value,item.data.content);
 assert.equal(p.node('memory-delete-confirm').hidden,true);
});
test('successful delete removes selected card, pagination uses server cursor',async()=>{
 const p=page();await load(p);await select(p);
 let work=p.node('memory-delete-preview').onclick();await p.reply(p.requests.at(-1),{token:'yes',affected_count:1,effect:'历史保留'});await work;
 work=p.node('memory-delete-confirm').onclick();await p.reply(p.requests.at(-1),{deleted:true});await work;
 assert.equal(p.node('memory-items').children.length,0);assert.equal(p.node('memory-editor').hidden,true);
 work=p.node('memory-next').onclick();assert.equal(p.requests.at(-1).arguments.after,'one');
 await p.reply(p.requests.at(-1),{records:[],next:null});await work;
 assert.equal(p.node('memory-prev').disabled,false);assert.equal(p.node('memory-next').disabled,true);
});

const explanation={root:'memories/one',total_nodes:1,total_issues:1,limited:false,meaning:'只读的程序来源记录',
 issues:[{resource:'memories/one',reason:'source_missing_or_changed',source:'<img src=x>'}],
 nodes:[{resource:'memories/one',classification:'personal',valid:true,owners:['合成账号'],conversation:'合成会话',audience:'group',source_count:1,sources:['<img src=x>']}],impact:{affected_count:2}};
test('source inspection is read-only, renders diagnostics as text, and invalidates on draft edits',async()=>{
 const p=page();await load(p);await select(p);const before=p.requests.length;
 const work=p.node('memory-explain').onclick();assert.equal(p.requests.at(-1).operation,'records.explain');
 await p.reply(p.requests.at(-1),explanation);await work;
 const text=p.node('memory-explanation').children.map(n=>n.textContent).join('\n');
 assert.match(text,/来源缺失或已发生变化/);assert.match(text,/<img src=x>/);assert.match(text,/可能影响2项/);assert.match(text,/共享范围：个人资料/);assert.match(text,/受众：群聊/);assert.match(p.node('memory-meta').textContent,/个人资料/);
 assert.equal(p.requests.length,before+1);assert.equal(p.node('memory-content').value,item.data.content);
 p.node('memory-content').oninput();assert.equal(p.node('memory-explanation').hidden,true);
 p.node('memory-explain').onclick();assert.equal(p.requests.length,before+1);
});
test('inspection failure clears old diagnostics; lock rejects late inspection and clears display',async()=>{
 const p=page();await load(p);await select(p);
 let work=p.node('memory-explain').onclick();await p.reply(p.requests.at(-1),explanation);await work;
 work=p.node('memory-explain').onclick();await p.reply(p.requests.at(-1),'来源检查失败',false);await work;
 assert.equal(p.node('memory-explanation').hidden,true);assert.match(p.node('memory-notice').textContent,/来源检查失败/);
 work=p.node('memory-explain').onclick();const request=p.requests.at(-1);p.update(false);
 await p.reply(request,explanation);await work;assert.equal(p.node('memory-explanation').hidden,true);
 assert.equal(p.node('memory-explanation').children.length,0);
});

const advancedPreview={token:'preview-one',changes:[{field:'content',before:{text:'"旧正文"',truncated:false},after:{text:'"新正文"',truncated:true}}],
 references:{added:['knowledge/two'],removed:['diaries/gone'],updated:['knowledge/three']},
 impact:{affected_count:3,sample:['memories/one','diaries/two'],sample_limited:true},
 changed:true,written:false,notice:'保存后保留旧版本；相关派生资料需重新检查。'};
const commits=p=>p.requests.filter(request=>request.operation==='records.advanced_commit').length;
async function previewImport(p,value=advancedPreview,pages=[['完整差异',null]]){
 const work=p.node('memory-advanced-import').onclick(),request=p.requests.at(-1);
 await p.reply(request,value);
 for(const [text,next] of pages)await p.reply(p.requests.at(-1),{text,next});
 await work;
}
test('an advanced preview clears and locks out a pending delete preview',async()=>{
 const p=page();await load(p);await select(p);
 let work=p.node('memory-delete-preview').onclick();
 await p.reply(p.requests.at(-1),{token:'delete-token',affected_count:2,effect:'历史保留'});await work;
 assert.equal(p.node('memory-delete-confirm').hidden,false);
 await previewImport(p);
 assert.equal(p.node('memory-delete-confirm').disabled,true);
 assert.equal(p.node('memory-delete-cancel').disabled,true);
 const count=p.requests.length;
 p.node('memory-delete-confirm').onclick();p.node('memory-delete-cancel').onclick();
 assert.equal(p.requests.length,count);
 work=p.node('memory-advanced-cancel').onclick();await p.reply(p.requests.at(-1),{cancelled:true});await work;
 assert.equal(p.node('memory-delete-confirm').hidden,true);assert.equal(p.node('memory-delete-cancel').hidden,true);
 p.node('memory-delete-confirm').onclick();
 assert.deepEqual(p.requests.filter(request=>request.operation==='records.delete'),[]);
 assert.equal(p.node('memory-content').value,item.data.content);
});
test('advanced export sends the displayed version and touches nothing else',async()=>{
 const p=page();await load(p);await select(p);const before=p.requests.length;
 const work=p.node('memory-advanced-export').onclick(),request=p.requests.at(-1);
 assert.equal(p.requests.length,before+1);assert.equal(request.action,'record-export');
 assert.equal(request.operation,undefined);assert.equal(request.path,undefined);
 assert.equal(request.kind,'memories');assert.equal(request.identifier,'one');assert.equal(request.expected_version,'v1');
 assert.match(p.node('memory-advanced-notice').textContent,/正在导出/);
 await p.reply(request,{saved:true,filename:'one.yaml',bytes:2048});await work;
 assert.match(p.node('memory-advanced-notice').textContent,/one\.yaml/);
 assert.match(p.node('memory-advanced-notice').textContent,/明文/);
 assert.equal(commits(p),0);assert.equal(p.requests.length,before+1);
});
test('advanced import pages the whole difference and never commits implicitly',async()=>{
 const p=page();await load(p);await select(p);
 const work=p.node('memory-advanced-import').onclick(),request=p.requests.at(-1);
 assert.equal(request.action,'record-import');assert.equal(request.path,undefined);
 assert.equal(request.kind,'memories');assert.equal(request.identifier,'one');assert.equal(request.expected_version,'v1');
 await p.reply(request,advancedPreview);
 const first=p.requests.at(-1);
 assert.equal(first.operation,'records.advanced_changes');assert.equal(first.arguments.token,'preview-one');assert.equal(first.arguments.offset,0);
 assert.equal(p.node('memory-advanced-confirm').disabled,true);
 assert.equal(commits(p),0);
 await p.reply(first,{text:'content\n修改前：旧\n',next:8000});
 const second=p.requests.at(-1);
 assert.equal(second.operation,'records.advanced_changes');assert.equal(second.arguments.offset,8000);
 assert.equal(p.node('memory-advanced-confirm').disabled,true);
 assert.equal(commits(p),0);
 await p.reply(second,{text:'修改后：新',next:null});await work;
 assert.equal(commits(p),0);assert.equal(p.node('memory-advanced-panel').hidden,false);
 assert.equal(p.node('memory-advanced-confirm').disabled,false);assert.equal(p.node('memory-advanced-cancel').disabled,false);
 assert.equal(p.node('memory-advanced-diff-text').textContent,'content\n修改前：旧\n修改后：新');
 assert.equal(p.node('memory-advanced-diff').hidden,false);
 const fields=p.node('memory-advanced-fields').children;
 assert.equal(fields.length,1);assert.match(fields[0].textContent,/旧正文/);assert.match(fields[0].textContent,/新正文/);
 assert.match(fields[0].textContent,/仅显示前 240 字/);
 assert.match(p.node('memory-advanced-references').textContent,/新增 1 项，移除 1 项，更新 1 项/);
 assert.match(p.node('memory-advanced-references').textContent,/knowledge\/two/);
 assert.match(p.node('memory-advanced-references').textContent,/diaries\/gone/);
 const impact=p.node('memory-advanced-impact').textContent;
 assert.match(impact,/影响 3 项/);assert.match(impact,/diaries\/two/);assert.match(impact,/仅列出前 2 项/);
 assert.match(p.node('memory-advanced-changed').textContent,/保存后保留旧版本/);
 assert.match(p.node('memory-advanced-summary').textContent,/即将导入记忆/);
});
test('advanced cancel writes nothing and confirm commits exactly one token',async()=>{
 const p=page();await load(p);await select(p);
 await previewImport(p);
 let work=p.node('memory-advanced-cancel').onclick(),request=p.requests.at(-1);
 assert.equal(request.operation,'records.advanced_cancel');assert.equal(request.arguments.token,'preview-one');
 await p.reply(request,{cancelled:true});await work;
 assert.equal(p.node('memory-advanced-panel').hidden,true);
 assert.match(p.node('memory-advanced-notice').textContent,/未改变/);
 assert.equal(p.node('memory-content').value,item.data.content);
 assert.equal(commits(p),0);
 await previewImport(p,{...advancedPreview,token:'preview-two'},[['差异二',null]]);
 work=p.node('memory-advanced-confirm').onclick();request=p.requests.at(-1);
 assert.equal(request.operation,'records.advanced_commit');assert.equal(request.arguments.token,'preview-two');
 await p.reply(request,{version:'v2',changed:true});
 const read=p.requests.at(-1);
 assert.equal(read.operation,'records.read');assert.equal(read.arguments.kind,'memories');assert.equal(read.arguments.identifier,'one');
 await p.reply(read,{...item,version:'v2',data:{...item.data,content:'新正文🙂'}});await work;
 assert.equal(commits(p),1);assert.match(p.node('memory-notice').textContent,/已提交/);
 assert.equal(p.node('memory-content').value,'新正文🙂');
 assert.equal(p.node('memory-advanced-panel').hidden,true);
 assert.equal(p.node('memory-advanced-confirm').disabled,true);
});
test('an open preview freezes editing switching and deleting',async()=>{
 const p=page();await load(p);await select(p);
 await previewImport(p);
 for(const id of ['memory-content','memory-tags','memory-kind','memory-save','memory-discard','memory-refresh',
  'memory-next','memory-prev','memory-delete-preview','memory-explain','memory-advanced-export','memory-advanced-import'])
  assert.equal(p.node(id).disabled,true,id+' must stay frozen while a preview is open');
 assert.equal(p.node('memory-items').children[0].disabled,true);
 const count=p.requests.length;
 p.node('memory-next').onclick();p.node('memory-prev').onclick();p.node('memory-refresh').onclick();
 p.node('memory-delete-preview').onclick();p.node('memory-save').onclick();p.node('memory-explain').onclick();
 p.node('memory-kind').value='knowledge';p.node('memory-kind').onchange();
 p.node('memory-items').children[0].onclick();
 p.node('memory-content').oninput();
 assert.equal(p.requests.length,count);assert.equal(p.node('memory-kind').value,'memories');
 assert.equal(p.node('memory-draft').textContent,'');assert.equal(p.node('memory-advanced-panel').hidden,false);
});
test('personal facts and unsaved drafts disable advanced import and export',async()=>{
 const facts=page();await loadFacts(facts);
 assert.equal(facts.node('memory-advanced-export').disabled,true);
 assert.equal(facts.node('memory-advanced-import').disabled,true);
 let count=facts.requests.length;
 facts.node('memory-advanced-export').onclick();facts.node('memory-advanced-import').onclick();
 assert.equal(facts.requests.length,count);
 const p=page();await load(p);await select(p);
 assert.equal(p.node('memory-advanced-export').disabled,false);assert.equal(p.node('memory-advanced-import').disabled,false);
 p.node('memory-content').value='未保存草稿';p.node('memory-content').oninput();
 assert.equal(p.node('memory-advanced-export').disabled,true);assert.equal(p.node('memory-advanced-import').disabled,true);
 count=p.requests.length;
 p.node('memory-advanced-export').onclick();p.node('memory-advanced-import').onclick();
 assert.equal(p.requests.length,count);assert.equal(commits(p),0);
});
test('a lock during the native picker drops the late preview without reading differences',async()=>{
 const p=page();await load(p);await select(p);
 const work=p.node('memory-advanced-import').onclick(),request=p.requests.at(-1);
 assert.equal(request.action,'record-import');
 p.update(false);
 const count=p.requests.length;
 await p.reply(request,advancedPreview);await work;
 assert.equal(p.requests.length,count);assert.equal(commits(p),0);
 assert.equal(p.node('memory-advanced-panel').hidden,true);
 assert.equal(p.node('memory-advanced-confirm').disabled,true);
 assert.equal(p.node('memory-advanced-notice').textContent,'');
 assert.equal(p.node('memory-editor').hidden,true);assert.equal(p.node('memory-items').children.length,0);
});
test('an incomplete difference keeps the confirmation unavailable',async()=>{
 const p=page();await load(p);await select(p);
 const work=p.node('memory-advanced-import').onclick();
 await p.reply(p.requests.at(-1),advancedPreview);
 const first=p.requests.at(-1);
 await p.reply(first,'差异读取失败',false);await work;
 assert.equal(p.node('memory-advanced-panel').hidden,true);
 assert.equal(p.node('memory-advanced-confirm').disabled,true);
 assert.match(p.node('memory-advanced-notice').textContent,/差异读取失败/);
 assert.equal(commits(p),0);
});
test('an unchanged file previews no difference and cannot be committed',async()=>{
 const p=page();await load(p);await select(p);
 await previewImport(p,{...advancedPreview,changed:false,changes:[],references:{added:[],removed:[],updated:[]}},[['',null]]);
 assert.equal(p.node('memory-advanced-confirm').disabled,true);
 assert.match(p.node('memory-advanced-notice').textContent,/没有需要保存的差异/);
 assert.match(p.node('memory-advanced-summary').textContent,/没有需要保存的差异/);
 assert.equal(p.node('memory-advanced-diff').hidden,true);
 assert.match(p.node('memory-advanced-fields').children[0].textContent,/没有变化/);
 assert.match(p.node('memory-advanced-references').textContent,/新增来源：无/);
 const count=p.requests.length;p.node('memory-advanced-confirm').onclick();assert.equal(p.requests.length,count);
});
