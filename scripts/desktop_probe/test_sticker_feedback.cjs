const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),events={},requests=[],listeners=[];
 const create=()=>({textContent:'',hidden:false,disabled:false,value:'',children:[],style:{},
  setAttribute(){},replaceChildren(){this.children=[];},append(...children){this.children.push(...children);}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create());return nodes.get(id);};
 let sequence=0;
 const webview={addEventListener(_,fn){listeners.push(fn);},postMessage(request){requests.push(request);}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/stickers.js'),'utf8'),{
  document:{getElementById:node,createElement:create},window:{chrome:{webview},addEventListener(name,fn){events[name]=fn;}},
  crypto:{randomUUID:()=>String(++sequence)},setTimeout:()=>sequence,clearTimeout(){}
 });
 const update=allowed=>events['qiban-management-state']({detail:{ready:true,locked:!allowed}});
 async function reply(request,result,ok=true){for(const fn of listeners)fn({data:{type:'management',request_id:request.request_id,response:ok?{ok:true,result}:{ok:false,message:result}}});await new Promise(resolve=>setImmediate(resolve));}
 return {node,requests,update,reply};
}
function find(root,id){if(root.id===id)return root;for(const child of root.children||[]){const result=find(child,id);if(result)return result;}}
const item={id:'one',revision:'v1',name:'<img onerror=evil>快乐',tags:['开心'],enabled:false,frames:1,preview:'data:image/png;base64,fixture'};
test('native import reloads and displays explicit feedback; no path supplied by page',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[]});
 p.node('sticker-name').value='快乐';p.node('sticker-tags').value='问候，开心';
 const work=p.node('sticker-import').onclick(),request=p.requests.at(-1);
 assert.equal(request.action,'sticker-import');assert.equal(request.path,undefined);assert.equal(request.tags.length,2);
 await p.reply(request,{duplicate:false,item});await p.reply(p.requests.at(-1),{items:[item]});await work;
 assert.match(p.node('sticker-notice').textContent,/已导入.*停用/);
 assert.equal(p.node('sticker-items').children[0].children[1].textContent,item.name+' · 已停用 · 静态图片');
});
test('enable and editing carry displayed revision, without frequency parameters',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[item]});
 const card=p.node('sticker-items').children[0];find(card,'sticker-edit-name-one').value='新名称';
 const work=find(card,'sticker-toggle-one').onclick(),request=p.requests.at(-1);
 assert.equal(request.operation,'stickers.configure');assert.equal(request.arguments.expected,'v1');
 assert.equal(request.arguments.enabled,true);assert.equal(request.arguments.name,'新名称');
 assert.equal(request.arguments.cooldown,undefined);
 await p.reply(request,{...item,enabled:true,revision:'v2'});await p.reply(p.requests.at(-1),{items:[{...item,enabled:true,revision:'v2'}]});await work;
 assert.match(p.node('sticker-notice').textContent,/已启用/);
});
test('cancel preserves library and lock clears previews and ignores late responses',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[item]});
 const work=p.node('sticker-import').onclick();await p.reply(p.requests.at(-1),'已取消导入，表情库未改变。',false);await work;
 assert.match(p.node('sticker-notice').textContent,/已取消/);assert.equal(p.node('sticker-items').children.length,1);
 const refresh=p.node('sticker-refresh').onclick(),request=p.requests.at(-1);p.update(false);
 await p.reply(request,{items:[item]});await refresh;
 assert.equal(p.node('sticker-items').children.length,0);assert.equal(p.node('sticker-name').value,'');
 assert.equal(p.node('sticker-import').disabled,true);
});

test('unchanged backend heartbeats preserve input nodes, edits and button identity',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[item]});
 const card=p.node('sticker-items').children[0],name=find(card,'sticker-edit-name-one'),tags=find(card,'sticker-edit-tags-one'),button=find(card,'sticker-toggle-one');
 tags.value='头像，自拍';tags.oninput();name.value='新的名称';name.oninput();
 for(let i=0;i<8;i++)p.update(true);
 assert.equal(p.node('sticker-items').children[0],card);assert.equal(find(card,'sticker-edit-tags-one'),tags);
 assert.equal(tags.value,'头像，自拍');assert.equal(button.disabled,false);assert.equal(p.requests.length,1);
 const work=button.onclick(),request=p.requests.at(-1);
 assert.equal(request.arguments.tags.join(','),'头像,自拍');assert.equal(request.arguments.name,'新的名称');
 assert.match(find(card,'sticker-feedback-one').textContent,/正在启用/);assert.equal(button.disabled,true);
 p.update(true);assert.equal(p.node('sticker-items').children[0],card);
 await p.reply(request,{...item,enabled:true});
 assert.match(find(card,'sticker-feedback-one').textContent,/已启用/);
 await p.reply(p.requests.at(-1),{items:[{...item,enabled:true,name:'新的名称',tags:['头像','自拍']}]});await work;
 assert.match(find(p.node('sticker-items').children[0],'sticker-feedback-one').textContent,/已启用/);
});
test('edit error preserves unsaved tags and shows the error beside the selected sticker',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[item]});
 const card=p.node('sticker-items').children[0],tags=find(card,'sticker-edit-tags-one');
 tags.value='新标签';tags.oninput();const work=find(card,'sticker-save-one').onclick();
 await p.reply(p.requests.at(-1),'资料已改变，请重新读取。',false);await work;p.update(true);
 assert.equal(p.node('sticker-items').children[0],card);assert.equal(tags.value,'新标签');assert.equal(tags.disabled,false);
 assert.match(find(card,'sticker-feedback-one').textContent,/资料已改变/);
});
test('manual reload keeps an unsaved draft instead of silently replacing it',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[item]});
 const tags=find(p.node('sticker-items').children[0],'sticker-edit-tags-one');tags.value='待保存';tags.oninput();
 const work=p.node('sticker-refresh').onclick();await p.reply(p.requests.at(-1),{items:[{...item,revision:'v2'}]});await work;
 assert.equal(find(p.node('sticker-items').children[0],'sticker-edit-tags-one').value,'待保存');
});


test('completed list reads clear progress, and reload errors retain successful mutation feedback',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[item]});
 assert.equal(p.node('sticker-notice').textContent,'表情列表已更新。');
 const card=p.node('sticker-items').children[0],work=find(card,'sticker-toggle-one').onclick();
 await p.reply(p.requests.at(-1),{...item,enabled:true,revision:'v2'});
 await p.reply(p.requests.at(-1),'后台暂时无法读取。',false);await work;
 assert.match(find(card,'sticker-feedback-one').textContent,/表情已启用。.*列表刷新未完成/);
 assert.equal(find(card,'sticker-toggle-one').disabled,false);
});
