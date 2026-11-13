const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),events={},requests=[],listeners=[];
 const create=()=>({textContent:'',hidden:false,disabled:false,value:'',children:[],
  replaceChildren(){this.children=[];},append(...children){this.children.push(...children);}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create());return nodes.get(id);};
 let sequence=0;
 const webview={addEventListener(_,fn){listeners.push(fn);},postMessage(request){requests.push(request);}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/media.js'),'utf8'),{
  document:{getElementById:node,createElement:create},window:{chrome:{webview},addEventListener(name,fn){events[name]=fn;}},
  crypto:{randomUUID:()=>String(++sequence)},setTimeout:()=>sequence,clearTimeout(){}
 });
 const settle=()=>new Promise(resolve=>setImmediate(resolve));
 const update=allowed=>events['qiban-management-state']({detail:{ready:true,locked:!allowed}});
 async function reply(request,result){for(const fn of listeners)fn({data:{type:'management',request_id:request.request_id,response:{ok:true,result}}});await settle();}
 return {node,requests,update,reply,settle};
}
const defaults={images:3,audio:2,files:1,single_mib:10,total_mib:20,audio_seconds:120};
const empty={items:[],settings:{days:30,revision:null},budget:{limits:defaults,revision:null},capacity:{mib:2048,revision:null,used_bytes:0,protected_bytes:0,reclaimable_bytes:0,over_limit:false}};
const item={id:'test',revision:'r1',kind:'file',name:'<img src=x onerror=evil>中文🙂',bytes:161,
 scope:{platform:'qq',account_identity:'10001',kind:'group',target_id:'10003'},status:'available',processed_at:1,preserve:[]};
test('capacity is displayed, saved with revision, and cleared on lock',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],empty);
 assert.equal(p.node('media-capacity').value,'2048');assert.match(p.node('media-capacity-usage').textContent,/受保护/);
 p.node('media-capacity').value='1024';const action=p.node('media-capacity-save').onclick(),request=p.requests.at(-1);
 assert.equal(request.operation,'media.capacity.configure');assert.equal(request.arguments.mib,1024);
 const changed={...empty.capacity,mib:1024,revision:'c2'};
 await p.reply(request,changed);await p.reply(p.requests.at(-1),{...empty,capacity:changed});await action;
 assert.match(p.node('media-capacity-notice').textContent,/已保存/);
 p.update(false);assert.equal(p.node('media-capacity-usage').textContent,'');assert.equal(p.node('media-capacity').value,'');
});
test('capacity reclamation is distinguished from expiry without implying text deletion',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{...empty,items:[{...item,status:'expired',cleanup_reason:'capacity'}]});
 assert.match(p.node('media-items').children[0].children[0].textContent,/因空间不足.*聊天记录保留/);
});
test('opening reads settings and never changes retention automatically',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],empty);
 assert.equal(p.requests.length,1);assert.equal(p.requests[0].operation,'media.list');
 assert.equal(p.node('media-days').value,'30');assert.equal(p.node('media-save').disabled,false);
});
test('budget saving uses revision and reloads; invalid or blank input is not sent',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],empty);
 p.node('media-images').value='';await p.node('media-budget-save').onclick();assert.equal(p.requests.length,1);
 p.node('media-images').value='4';const action=p.node('media-budget-save').onclick(),request=p.requests.at(-1);
 assert.equal(request.operation,'media.budget.configure');assert.equal(request.arguments.expected,null);
 assert.equal(request.arguments.limits.images,4);assert.equal(request.arguments.limits.audio_seconds,120);
 const saved={limits:{...defaults,images:4},revision:'new'};
 await p.reply(request,saved);await p.reply(p.requests.at(-1),{...empty,budget:saved});await action;
 assert.equal(p.node('media-images').value,'4');assert.match(p.node('media-budget-notice').textContent,/上限已保存/);
 p.update(false);assert.equal(p.node('media-images').value,'');assert.equal(p.node('media-budget-save').disabled,true);
});
test('saving uses the displayed revision, gives feedback, and reloads persisted settings',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],empty);p.node('media-days').value='45';
 const action=p.node('media-save').onclick();const request=p.requests.at(-1);
 assert.equal(request.operation,'media.configure');assert.equal(request.arguments.days,45);assert.equal(request.arguments.expected,null);
 await p.reply(request,{days:45,revision:'r2'});await p.reply(p.requests.at(-1),{...empty,settings:{days:45,revision:'r2'}});await action;
 assert.equal(p.node('media-days').value,'45');assert.match(p.node('media-notice').textContent,/已保存.*45/);
});
test('lock clears names and rejects late listing responses',async()=>{
 const p=page();p.update(true);const first=p.requests[0];p.update(false);
 await p.reply(first,{...empty,items:[item]});assert.equal(p.node('media-items').children.length,0);
 assert.equal(p.node('media-days').value,'');assert.equal(p.node('media-save').disabled,true);
});
test('file name is text and cancelling favorite preserves other long-term reasons',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{...empty,items:[{...item,preserve:['saved','favorite']}]});
 const row=p.node('media-items').children[0];assert.match(row.children[0].textContent,/<img.*中文🙂/);
 const action=row.children[2].onclick();const request=p.requests.at(-1);
 assert.equal(request.operation,'media.preserve');assert.equal(JSON.stringify(request.arguments.reasons),'["saved"]');
 await p.reply(request,{});await p.reply(p.requests.at(-1),{...empty,items:[{...item,preserve:['saved']}]});await action;
 assert.match(p.node('media-items').children[0].children[0].textContent,/长期保留/);
});
test('expired originals cannot be favorited and maintenance failure is visible',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{...empty,items:[{...item,status:'expired'}],maintenance_error:'database_error'});
 assert.equal(p.node('media-items').children[0].children[2].disabled,true);
 assert.match(p.node('media-notice').textContent,/自动清理未完成/);
});


test('voice reply opt-in and outgoing duration save with visible feedback and lock reset',async()=>{
 const p=page(),voice={enabled:false,seconds:30,revision:null};
 p.update(true);await p.reply(p.requests[0],{...empty,voice});
 assert.equal(p.node('media-voice-enabled').checked,false);
 assert.equal(p.node('media-voice-seconds').value,'30');
 p.node('media-voice-enabled').checked=true;p.node('media-voice-seconds').value='60';
 const action=p.node('media-voice-save').onclick(),request=p.requests.at(-1);
 assert.equal(request.operation,'media.voice.configure');assert.equal(request.arguments.enabled,true);
 assert.equal(request.arguments.seconds,60);
 const changed={enabled:true,seconds:60,revision:'v1'};
 await p.reply(request,changed);await p.reply(p.requests.at(-1),{...empty,voice:changed});await action;
 assert.match(p.node('media-voice-notice').textContent,/已保存/);
 p.update(false);assert.equal(p.node('media-voice-enabled').checked,false);
 assert.equal(p.node('media-voice-seconds').value,'');
});
