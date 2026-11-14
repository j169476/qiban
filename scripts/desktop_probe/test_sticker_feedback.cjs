// 表情包库：中间栏只有“选择这张表情”的入口，名称/标签/启用状态只在一处编辑（右栏），
// 只有一个“保存更改”，一次提交一个 stickers.configure 请求，未保存输入跨选择与心跳保留。
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),events={},requests=[],listeners=[];
 const create=()=>({textContent:'',hidden:false,disabled:false,value:'',checked:false,src:'',className:'',attributes:{},children:[],
  setAttribute(name,value){this.attributes[name]=String(value);},
  getAttribute(name){return this.attributes[name];},
  removeAttribute(name){delete this.attributes[name];if(name==='src')this.src='';},
  replaceChildren(...items){this.children=items;},
  append(...items){this.children.push(...items);}});
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
const item={id:'one',revision:'v1',name:'快乐',tags:['开心'],enabled:false,frames:1,preview:'data:image/png;base64,fixture'};
const other={...item,id:'two',revision:'w1',name:'生气',tags:['不爽'],enabled:true};
function saveLabels(list){return list.children.map(button=>button.children.map?button.children.map(child=>child.textContent).join(' '):'');}
test('native import reloads and displays explicit feedback; no path supplied by page',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[]});
 p.node('sticker-name').value='快乐';p.node('sticker-tags').value='问候，开心';
 const work=p.node('sticker-import').onclick(),request=p.requests.at(-1);
 assert.equal(request.action,'sticker-import');assert.equal(request.path,undefined);assert.equal(request.tags.join(','),'问候,开心');
 await p.reply(request,{duplicate:false,item});await p.reply(p.requests.at(-1),{items:[item]});await work;
 assert.match(p.node('sticker-notice').textContent,/已导入.*停用/);
 assert.match(p.node('sticker-items').children[0].children[1].textContent,/快乐.*已停用.*静态图片/);
});
test('one edit place and one 保存更改 sends name, tags and enable state in a single request',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[item]});
 p.node('sticker-items').children[0].onclick();
 assert.equal(p.node('sticker-detail-name').value,'快乐');
 assert.equal(p.node('sticker-detail-save').disabled,true,'没有改动时保存不可用');
 // 列表里的每张卡片只是选择入口，自身不带编辑框或保存按钮。
 for(const button of p.node('sticker-items').children)
  assert.match(button.getAttribute('aria-label'),/^选择表情：/);
 p.node('sticker-detail-name').value='新名称';p.node('sticker-detail-name').oninput();
 p.node('sticker-detail-tags').value='头像，自拍';p.node('sticker-detail-tags').oninput();
 p.node('sticker-detail-enabled').checked=true;p.node('sticker-detail-enabled').onchange();
 assert.match(p.node('sticker-detail-feedback').textContent,/未保存的更改/);
 assert.equal(p.node('sticker-detail-save').disabled,false);
 const before=p.requests.length;
 const work=p.node('sticker-detail-save').onclick(),request=p.requests.at(-1);
 assert.equal(p.requests.length,before+1,'保存只发一个请求');
 assert.equal(request.operation,'stickers.configure');
 const args=request.arguments;
 assert.equal(args.identifier,'one');
 assert.equal(args.name,'新名称');
 assert.equal(args.tags.join(','),'头像,自拍');
 assert.equal(args.enabled,true);
 assert.equal(args.expected,'v1');
 await p.reply(request,{...item,enabled:true,revision:'v2'});
 await p.reply(p.requests.at(-1),{items:[{...item,enabled:true,revision:'v2',name:'新名称',tags:['头像','自拍']}]});await work;
 assert.match(p.node('sticker-notice').textContent,/更改已保存，表情已启用/);
 assert.equal(p.node('sticker-detail-save').disabled,true,'保存完成后不再显示未保存');
});
test('unsaved edits survive switching the selected sticker and unchanged heartbeats',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[item,other]});
 let picks=p.node('sticker-items').children;
 picks[0].onclick();
 p.node('sticker-detail-name').value='快乐二';p.node('sticker-detail-name').oninput();
 picks[1].onclick();
 assert.equal(p.node('sticker-detail-name').value,'生气');
 p.node('sticker-detail-enabled').checked=false;p.node('sticker-detail-enabled').onchange();
 const before=p.requests.length;
 for(let i=0;i<6;i++)p.update(true);
 assert.equal(p.requests.length,before,'心跳不重新读取、也不覆盖草稿');
 assert.equal(p.node('sticker-items').children[0],picks[0]);
 picks[0].onclick();
 assert.equal(p.node('sticker-detail-name').value,'快乐二');
 picks[1].onclick();
 assert.equal(p.node('sticker-detail-enabled').checked,false);
 assert.match(p.node('sticker-detail-feedback').textContent,/未保存的更改/);
});
test('a failed save keeps the unsaved fields and shows the error next to the selected sticker',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[item]});
 p.node('sticker-items').children[0].onclick();
 p.node('sticker-detail-tags').value='新标签';p.node('sticker-detail-tags').oninput();
 const work=p.node('sticker-detail-save').onclick();
 await p.reply(p.requests.at(-1),'资料已改变，请重新读取。',false);await work;
 assert.equal(p.node('sticker-detail-tags').value,'新标签');
 assert.equal(p.node('sticker-detail-tags').disabled,false);
 assert.match(p.node('sticker-detail-feedback').textContent,/资料已改变/);
 assert.equal(p.node('sticker-detail-save').disabled,false);
});
test('manual reload keeps an unsaved draft instead of silently replacing it',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[item]});
 p.node('sticker-items').children[0].onclick();
 p.node('sticker-detail-name').value='待保存';p.node('sticker-detail-name').oninput();
 const work=p.node('sticker-refresh').onclick();
 await p.reply(p.requests.at(-1),{items:[{...item,revision:'v2'}]});await work;
 assert.equal(p.node('sticker-detail-name').value,'待保存');
 assert.equal(p.node('sticker-detail-save').disabled,false);
});
test('lock clears drafts and previews and ignores a late list answer',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{items:[item]});
 p.node('sticker-items').children[0].onclick();
 p.node('sticker-detail-name').value='未保存';p.node('sticker-detail-name').oninput();
 const work=p.node('sticker-refresh').onclick(),request=p.requests.at(-1);
 p.update(false);
 assert.equal(p.node('sticker-items').children.length,0);
 assert.equal(p.node('sticker-detail-name').value,'');
 assert.equal(p.node('sticker-detail-save').disabled,true);
 assert.equal(p.node('sticker-detail-preview').hidden,true);
 await p.reply(request,{items:[item]});await work;
 assert.equal(p.node('sticker-items').children.length,0);
 assert.equal(p.node('sticker-detail-preview').hidden,true);
 assert.deepEqual(saveLabels(p.node('sticker-items')),[]);
});
