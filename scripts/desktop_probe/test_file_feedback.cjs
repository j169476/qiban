const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),events={},requests=[],listeners=[];
 const create=()=>({textContent:'',hidden:false,disabled:false,value:'',children:[],style:{},
  setAttribute(){},replaceChildren(){this.children=[];},append(...children){this.children.push(...children);}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create());return nodes.get(id);};
 let sequence=0;
 const webview={addEventListener(_,fn){listeners.push(fn);},postMessage(request){requests.push(request);}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/files.js'),'utf8'),{
  document:{getElementById:node,createElement:create},window:{chrome:{webview},addEventListener(name,fn){events[name]=fn;}},
  crypto:{randomUUID:()=>String(++sequence)},setTimeout:()=>sequence,clearTimeout(){}
 });
 const update=allowed=>events['qiban-management-state']({detail:{ready:true,locked:!allowed}});
 async function reply(request,result,ok=true){for(const fn of listeners)fn({data:{type:'management',request_id:request.request_id,response:ok?{ok:true,result}:{ok:false,message:result}}});await new Promise(resolve=>setImmediate(resolve));}
 return {node,requests,update,reply};
}
function find(root,id){if(root.id===id)return root;for(const child of root.children||[]){const result=find(child,id);if(result)return result;}}

const scope={platform:'qq',account_identity:'80001',kind:'group',target_id:'90001'};
const item={id:'one',revision:'v1',name:'说明.txt',tags:['说明'],enabled:false,bytes:123,scopes:[]};
async function load(p,items=[item]){await p.reply(p.requests.at(-1),{items});await p.reply(p.requests.at(-1),{items:[{scope}]});}
function checkbox(card){return card.children.find(c=>c.children?.some(x=>x.textContent==='允许使用的会话')).children[1].children[0];}

test('file import uses native picker, has no page path and reports success',async()=>{
 const p=page();p.update(true);await load(p,[]);
 const work=p.node('file-import').onclick(),req=p.requests.at(-1);
 assert.equal(req.action,'file-import');assert.equal(req.path,undefined);
 await p.reply(req,{item,duplicate:false});await load(p);await work;
 assert.match(p.node('file-notice').textContent,/已导入.*停用/);
});
test('scope checkbox and edits survive heartbeat and failure; unified save submits exactly chosen scope and enabled draft',async()=>{
 const p=page();p.update(true);await load(p);
 const card=p.node('file-items').children[0],choice=checkbox(card);
 choice.checked=true;choice.onchange();
 const name=find(card,'file-edit-name-one');name.value='新的说明.txt';name.oninput();
 for(let i=0;i<5;i++)p.update(true);
 assert.equal(p.node('file-items').children[0],card);
 const enabled=find(card,'file-edit-enabled-one');enabled.checked=true;enabled.onchange();
 const work=find(card,'file-save-one').onclick(),req=p.requests.at(-1);
 assert.equal(req.operation,'files.configure');assert.equal(req.arguments.allowed_scopes.length,1);
 assert.equal(JSON.stringify(req.arguments.allowed_scopes[0]),JSON.stringify(scope));
 assert.equal(req.arguments.name,'新的说明.txt');assert.equal(req.arguments.enabled,true);
 await p.reply(req,'会话授权已改变。',false);await work;
 assert.equal(choice.checked,true);assert.equal(name.value,'新的说明.txt');assert.equal(enabled.checked,true);
 assert.match(find(card,'file-feedback-one').textContent,/授权已改变/);
});
test('successful enable remains visible even if list refresh fails',async()=>{
 const p=page();p.update(true);await load(p);
 const card=p.node('file-items').children[0];checkbox(card).checked=true;
 const enabled=find(card,'file-edit-enabled-one');enabled.checked=true;enabled.onchange();
 const work=find(card,'file-save-one').onclick();await p.reply(p.requests.at(-1),{...item,enabled:true});
 await p.reply(p.requests.at(-1),'读取失败',false);await work;
 assert.match(find(card,'file-feedback-one').textContent,/已启用.*刷新未完成/);
});
test('cancel keeps items; lock clears drafts and discards late list without more reads',async()=>{
 const p=page();p.update(true);await load(p);
 const work=p.node('file-import').onclick();await p.reply(p.requests.at(-1),'已取消导入，文件库未改变。',false);await work;
 assert.equal(p.node('file-items').children.length,1);assert.match(p.node('file-notice').textContent,/已取消/);
 const refresh=p.node('file-refresh').onclick(),req=p.requests.at(-1),count=p.requests.length;p.update(false);
 await p.reply(req,{items:[item]});await refresh;
 assert.equal(p.requests.length,count);assert.equal(p.node('file-items').children.length,0);
 assert.equal(p.node('file-import').disabled,true);
});

test('outgoing file limits save with displayed revision and reject invalid input',async()=>{
 const p=page();p.update(true);
 await p.reply(p.requests.at(-1),{items:[],settings:{count:3,total_mib:20,revision:'limits1'}});
 await p.reply(p.requests.at(-1),{items:[]});
 p.node('file-count').value='4';p.node('file-total-mib').value='30';
 const work=p.node('file-settings-save').onclick(),req=p.requests.at(-1);
 assert.equal(req.operation,'files.settings.configure');assert.equal(req.arguments.expected,'limits1');assert.equal(req.arguments.count,4);
 await p.reply(req,{count:4,total_mib:30,revision:'limits2'});
 await p.reply(p.requests.at(-1),{items:[],settings:{count:4,total_mib:30,revision:'limits2'}});
 await p.reply(p.requests.at(-1),{items:[]});await work;
 assert.match(p.node('file-notice').textContent,/上限已保存/);
 const count=p.requests.length;p.node('file-count').value='';p.node('file-settings-save').onclick();
 assert.equal(p.requests.length,count);assert.match(p.node('file-notice').textContent,/整数/);
 p.update(false);assert.equal(p.node('file-count').value,'');
});

 test('enabled edits do not submit until save; saving all fields can disable a file',async()=>{
 const p=page();p.update(true);await load(p,[{...item,enabled:true,scopes:[scope]}]);
 const card=p.node('file-items').children[0],enabled=find(card,'file-edit-enabled-one');
 assert.equal(enabled.checked,true);assert.equal(find(card,'file-toggle-one'),undefined);
 const count=p.requests.length;enabled.checked=false;enabled.onchange();
 for(let i=0;i<3;i++)p.update(true);
 assert.equal(p.requests.length,count);assert.equal(enabled.checked,false);
 const work=find(card,'file-save-one').onclick(),req=p.requests.at(-1);
 assert.equal(req.arguments.enabled,false);assert.equal(req.arguments.name,item.name);
 assert.equal(req.arguments.allowed_scopes.length,1);
 await p.reply(req,{...item,enabled:false,revision:'v2'});
 await load(p,[{...item,enabled:false,revision:'v2',scopes:[scope]}]);await work;
 assert.match(p.node('file-notice').textContent,/更改已保存.*已停用/);
 assert.equal(find(p.node('file-items').children[0],'file-edit-enabled-one').checked,false);
 });
