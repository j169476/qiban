const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const original={version:'v1',data:{name:'合成人物',core:{description:'原简介',speaking_style:['自然交流']},interests:['读书'],locked_fields:['name','core'],foundation_memories:['foundation'],relationship_baseline:{owner_uid:'synthetic'}}};
function page(initial=original){
 const nodes=new Map(),listeners=[],requests=[];let sequence=0,card=structuredClone(initial),locked=false;
 const create=()=>({textContent:'',value:'',children:[],dataset:{},disabled:false,hidden:false,open:false,
  append(...items){this.children.push(...items);},replaceChildren(){this.children=[];},showModal(){this.open=true;},close(){this.open=false;}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create());return nodes.get(id);};
 const replies={
  'session.status':()=>({locked,lock_enabled:true}),
  'session.lock':()=>{locked=true;return {locked,lock_enabled:true};},
  'session.unlock':()=>{locked=false;return {locked,lock_enabled:true};},
  'config.read':()=>structuredClone(card),
  'config.patch':args=>{card={version:'v2',data:{...card.data,...args.changes}};return {version:card.version};},
  'backups.list':()=>({backups:[]}), 'runtime.status':()=>({platforms:[]}), 'notifications.page':()=>({items:[],unread:0,total:0,page:1,pages:1})
 };
 const webview={addEventListener(_,f){listeners.push(f);},postMessage(request){requests.push(request);Promise.resolve().then(()=>replies[request.operation](request.arguments)).then(
  result=>listeners.forEach(f=>f({data:{type:'management',request_id:request.request_id,response:{ok:true,result}}})),
  error=>listeners.forEach(f=>f({data:{type:'management',request_id:request.request_id,response:{ok:false,message:error.message}}})));
 }};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/management.js'),'utf8'),{
  document:{getElementById:node,createElement:create,createTextNode:text=>({textContent:text})},
  window:{chrome:{webview},dispatchEvent(){},addEventListener(){}},
  CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail;}},
  crypto:{randomUUID:()=>String(++sequence)},Date:class extends Date{static now(){return 10000;}},setTimeout:()=>sequence,clearTimeout(){}
 });
 const settle=()=>new Promise(resolve=>setImmediate(resolve));
 async function update(ready){listeners.forEach(f=>f({data:{type:'service',management:true,ready}}));await settle();}
 function edit(id,value){node(id).value=value;node(id).oninput();}
 return {node,replies,requests,update,edit,settle};
}
test('one save updates name, description, speech and interests with current version',async()=>{
 const p=page();await p.update(true);
 p.edit('persona-name','合成人物🙂');p.edit('persona-description','新的简介');
 p.edit('persona-speaking','自然\n说清楚数字123');p.edit('persona-interests','读书\n绘画');
 const reads=p.requests.filter(r=>r.operation==='config.read').length;
 const saved=p.node('save-persona').onclick();p.node('save-persona').onclick();await saved;
 const writes=p.requests.filter(r=>r.operation==='config.patch');assert.equal(writes.length,1);
 assert.equal(writes[0].arguments.expected,'v1');
 assert.deepEqual(JSON.parse(JSON.stringify(writes[0].arguments.changes.core.speaking_style)),['自然','说清楚数字123']);
 assert.deepEqual(JSON.parse(JSON.stringify(writes[0].arguments.changes.interests)),['读书','绘画']);
 assert.equal(writes[0].arguments.changes.foundation_memories,undefined);
 assert.equal(writes[0].arguments.changes.relationship_baseline,undefined);
 assert.equal(p.requests.filter(r=>r.operation==='config.read').length,reads);
 assert.match(p.node('profile-notice').textContent,/已保存/);
 p.edit('persona-name','第二次');await p.node('save-persona').onclick();
 assert.equal(p.requests.at(-1).arguments.expected,'v2');
});
test('version failure and heartbeats keep drafts; reload waits for explicit discard',async()=>{
 const p=page();await p.update(true);p.edit('persona-speaking','未保存的说话方式');
 p.replies['config.patch']=()=>{throw Error('版本冲突');};
 await p.node('save-persona').onclick();await p.update(true);
 const count=p.requests.length;await p.node('reload-persona').onclick();
 assert.equal(p.requests.length,count);assert.equal(p.node('persona-speaking').value,'未保存的说话方式');
 assert.match(p.node('profile-notice').textContent,/版本冲突/);
 p.node('discard-persona').onclick();assert.equal(p.node('persona-speaking').value,'自然交流');
 assert.equal(p.node('reload-persona').disabled,false);
});
test('offline clears private form and drops a late profile read',async()=>{
 const p=page();await p.update(true);let resolve;
 p.replies['config.read']=()=>new Promise(r=>{resolve=r;});
 const pending=p.node('reload-persona').onclick();await p.settle();await p.update(false);
 resolve(original);await pending;
 for(const id of ['persona-name','persona-description','persona-speaking','persona-interests'])assert.equal(p.node(id).value,'');
 assert.equal(p.node('profile-area').dataset.loaded,'false');
});
test('lock while saving prevents late response from restoring fields',async()=>{
 const p=page();await p.update(true);p.edit('persona-name','待保存');let resolve;
 p.replies['config.patch']=()=>new Promise(r=>{resolve=r;});
 const pending=p.node('save-persona').onclick();await p.settle();await p.node('lock-now').onclick();
 resolve({version:'late'});await pending;
 assert.equal(p.node('persona-name').value,'');assert.equal(p.node('profile-notice').textContent,'');
 assert.equal(p.node('save-persona').disabled,true);
});
test('list bounds reject before writing and preserve the draft',async()=>{
 const p=page();await p.update(true);const text=Array.from({length:101},(_,i)=>'项'+i).join('\n');
 p.edit('persona-interests',text);await p.node('save-persona').onclick();
 assert.equal(p.requests.some(r=>r.operation==='config.patch'),false);
 assert.equal(p.node('persona-interests').value,text);assert.match(p.node('profile-notice').textContent,/100项/);
});
test('editing only name preserves unchanged legacy list strings exactly',async()=>{
 const card=structuredClone(original);card.data.core.speaking_style=[' 包含空格\n和换行 '];
 const p=page(card);await p.update(true);p.edit('persona-name','仅改名字');await p.node('save-persona').onclick();
 assert.equal(p.requests.at(-1).arguments.changes.core.speaking_style[0],card.data.core.speaking_style[0]);
 assert.equal(p.requests.at(-1).arguments.changes.core.speaking_style.length,1);
});

test('inbox page selection, read, confirmed deletion and clamped last page',async()=>{
 const p=page();
 const items=Array.from({length:11},(_,i)=>({id:String(i),platform:'qq',message:'测试',read:false,created_at:'2026-11-18T03:00:00Z'}));
 p.replies['notifications.page']=args=>{
  const pages=Math.max(1,Math.ceil(items.length/10)),page=Math.min(args.page,pages);
  return {total:items.length,unread:items.filter(i=>!i.read).length,page,pages,items:structuredClone(items.slice((page-1)*10,page*10))};
 };
 p.replies['notifications.update']=({action,identifiers})=>{
  for(const id of identifiers){
   const index=items.findIndex(i=>i.id===id);
   if(action==='delete')items.splice(index,1);else items[index].read=true;
  }
  return {changed:identifiers.length};
 };
 await p.update(true);
 const select=()=>{p.node('inbox-select').checked=true;p.node('inbox-select').onchange();};
 assert.equal(p.node('notification-items').children.length,10);
 select();await p.node('inbox-read').onclick();assert.equal(p.node('notification-title').textContent,'应用通知 · 1 条未读');
 p.node('inbox-jump').value='2';await p.node('inbox-go').onclick();
 assert.equal(p.node('notification-items').children.length,1);
 select();await p.node('inbox-delete').onclick();assert.equal(items.length,11);
 p.node('inbox-cancel').onclick();assert.equal(items.length,11);
 await p.node('inbox-delete').onclick();await p.node('inbox-delete').onclick();
 assert.equal(items.length,10);assert.equal(p.node('inbox-pages').hidden,true);
 assert.equal(p.node('notification-title').textContent,'应用通知 · 0 条未读');
});
