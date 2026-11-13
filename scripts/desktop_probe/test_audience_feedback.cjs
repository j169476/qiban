// QQ management UI tests use only synthetic local replies.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),events={},requests=[],listeners=[];
 const node=id=>{
  if(!nodes.has(id))nodes.set(id,{textContent:'',hidden:false,disabled:false,value:'',children:[],
   replaceChildren(){this.children=[];this.value='';},append(child){this.children.push(child);},
   removeAttribute(name){delete this[name];}});
  return nodes.get(id);
 };
 let sequence=0,tick;
 const webview={addEventListener(_,fn){listeners.push(fn);},postMessage(request){requests.push(request);}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/audience.js'),'utf8'),{
  document:{getElementById:node,createElement:()=>({children:[],append(...children){this.children.push(...children);}})},window:{chrome:{webview},addEventListener(name,fn){events[name]=fn;}},
  crypto:{randomUUID:()=>String(++sequence)},setTimeout:()=>sequence,clearTimeout(){},setInterval(fn){tick=fn;}
 });
 const settle=()=>new Promise(resolve=>setImmediate(resolve));
 const update=allowed=>events['qiban-management-state']({detail:{ready:true,locked:!allowed}});
 async function reply(request,result){for(const fn of listeners)fn({data:{type:'management',request_id:request.request_id,response:{ok:true,result}}});await settle();}
 return {node,requests,update,reply,settle,tick:()=>tick()};
}
test('WeChat detection is explicit and a reply arriving after lock cannot reveal accounts', async()=>{
 const p=page();p.node('audience-platform').value='wechat';p.update(true);
 assert.equal(p.requests.length,0);
 const detection=p.node('audience-detect').onclick();
 assert.equal(p.requests.at(-1).operation,'wechat.accounts');
 p.update(false);await p.reply(p.requests.at(-1),{accounts:['synthetic']});await detection;
 assert.equal(p.node('audience-accounts').textContent,'');
 p.update(true);
 const again=p.node('audience-detect').onclick();
 await p.reply(p.requests.at(-1),{accounts:[]});await again;
 assert.match(p.node('audience-accounts').textContent,/先在电脑微信登录/);
});
test('WeChat switches only on explicit selection and clears directory when account changes', async()=>{
 const p=page(),el=id=>p.node('audience-'+id);
 el('platform').value='wechat';p.update(true);
 let job=el('detect').onclick();
 await p.reply(p.requests.at(-1),{accounts:['one','two'],profiles:{accounts:['one'],selected:'one',revision:1}});await job;
 assert.equal(p.requests.length,1);assert.equal(el('select').disabled,true);
 job=el('groups').onclick();
 assert.deepEqual(JSON.parse(JSON.stringify(p.requests.at(-1).arguments)),{login:'one',expected:1,kind:'group',after:''});
 await p.reply(p.requests.at(-1),{items:[{name:'合成群',username:'target'}],next:null});await job;
 assert.equal(el('members').children.length,1);el('login').value='two';el('login').onchange();
 job=el('select').onclick();assert.equal(p.requests.at(-1).operation,'wechat.select');
 await p.reply(p.requests.at(-1),{accounts:['one','two'],selected:'two',revision:2});await job;
 assert.equal(el('members').children.length,0);assert.match(el('accounts').textContent,/two/);
});
test('WeChat bulk verification keeps prior pages and distinguishes equal labels by ID', async()=>{
 const p=page(),el=id=>p.node('audience-'+id);el('platform').value='wechat';p.update(true);
 let job=el('detect').onclick();
 await p.reply(p.requests.at(-1),{accounts:['one'],profiles:{accounts:['one'],selected:'one',revision:3}});await job;
 for(const [button,username] of [['friends','first'],['groups','second@chatroom']]){
  job=el(button).onclick();
  await p.reply(p.requests.at(-1),{items:[{username,name:'同名'}],next:null});await job;
  el('check-page').onclick();
 }
 job=el('verify').onclick();const req=p.requests.at(-1);
 assert.equal(req.operation,'wechat.targets.verify');
 assert.deepEqual(JSON.parse(JSON.stringify(req.arguments.targets)),[
  {kind:'private',username:'first'},{kind:'group',username:'second@chatroom'}]);
 await p.reply(req,{verified:1,unavailable:['second@chatroom']});await job;
 assert.match(el('selection-info').textContent,/同名 · second@chatroom/);
 el('uncheck-page').onclick();assert.match(el('selection-info').textContent,/已勾选 1/);
 el('clear').onclick();assert.equal(el('verify').disabled,true);
});
test('a retained unavailable friend can be removed explicitly and an empty list saved', async()=>{
 const p=page(),el=id=>p.node('audience-'+id);
 el('platform').value='wechat';p.update(true);
 let job=el('detect').onclick();
 await p.reply(p.requests.at(-1),{accounts:['one'],profiles:{accounts:['one'],selected:'one',revision:1}});await job;
 assert.equal(el('save-list').disabled,true);
 job=el('read-list').onclick();
 await p.reply(p.requests.at(-1),{version:'old',targets:[{kind:'private',username:'gone'}]});await job;
 assert.equal(el('chosen-list').children[0].value,'gone');
 el('chosen-list').value='gone';el('remove-one').onclick();
 job=el('save-list').onclick();const req=p.requests.at(-1);
 assert.equal(req.operation,'wechat.targets.save');
 assert.deepEqual(JSON.parse(JSON.stringify(req.arguments)),{login:'one',expected:1,version:'old',targets:[]});
 await p.reply(req,{version:'new',targets:[]});await job;
 assert.match(el('selection-info').textContent,/已保存，共 0/);
});
test('group risk is shown before any save; cancel leaves the account unchanged', async()=>{
 const p=page();p.node('audience-platform').value='qq';p.update(true);
 assert.equal(p.requests.length,0);
 const load=p.node('audience-load').onclick();
 await p.reply(p.requests.at(-1),{ready:true,account:'12345',categories:{private:{enabled:false},group:{enabled:false}}});await load;
 const open=p.node('audience-group').onclick();const req=p.requests.at(-1);
 assert.deepEqual(JSON.parse(JSON.stringify(req.arguments)),{platform:'qq',kind:'group',account:'12345',enabled:true});
 const preview={token:'t',scope:{kind:'group'},message:'高风险：QQ 账号12345，群成员可见，消耗额度',button:'确认允许所有群'};
 await p.reply(req,preview);await open;
 assert.equal(p.node('audience-preview').hidden,false);
 assert.match(p.node('audience-warning').textContent,/群成员可见/);
 p.node('audience-cancel').onclick();
 assert.equal(p.node('audience-preview').hidden,true);assert.equal(p.requests.length,2);
 const again=p.node('audience-group').onclick();await p.reply(p.requests.at(-1),preview);await again;
 const save=p.node('audience-confirm').onclick();assert.equal(p.requests.at(-1).operation,'audience.save');
 await p.reply(p.requests.at(-1),{enabled:true});await save;
 assert.match(p.node('audience-group').textContent,/关闭/);assert.equal(p.node('audience-private').textContent,'允许所有好友');
});
