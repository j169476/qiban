const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');

function page(){
 const nodes=new Map(),events={},requests=[];let inbound,sequence=0;
 function node(){return {value:'',textContent:'',children:[],open:false,disabled:false,
  append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},
  addEventListener(){},setAttribute(){},showModal(){this.open=true;},close(){this.open=false;}};}
 const id=name=>{if(!nodes.has(name))nodes.set(name,node());return nodes.get(name);};
 const webview={postMessage:req=>requests.push(req),postMessageWithAdditionalObjects:(req,files)=>requests.push({...req,files}),
  addEventListener:(_,fn)=>{inbound=fn;}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/room.js'),'utf8'),{
  document:{getElementById:id,createElement:node},window:{chrome:{webview},addEventListener:(name,fn)=>events[name]=fn},
  crypto:{randomUUID:()=>String(++sequence).padStart(32,'0'),subtle:require('node:crypto').webcrypto.subtle},setTimeout(){},clearTimeout(){},setInterval(){},clearInterval(){}
 });
 return {el:name=>id('room-'+name),requests,
  state:session=>events['qiban-management-state']({detail:{ready:true,locked:!session,roomSession:session,lockEnabled:true}}),
  fail(request){inbound({data:{type:'management',request_id:request.request_id,response:{ok:false,message:'请重试'}}});},
  async reply(request,result){inbound({data:{type:'management',request_id:request.request_id,response:{ok:true,result}}});await new Promise(r=>setImmediate(r));}};
}
test('lock clears displayed history and draft; delayed reads and sends cannot repopulate a reopened room',async()=>{
 const p=page(),el=p.el;
 p.state('first');el('open').onclick();
 const rows={messages:[{speaker:'user',text:'旧会话中文🙂'}],next:''};
 await p.reply(p.requests.at(-1),rows);assert.equal(el('history').children[0].children[1].textContent,'旧会话中文🙂');
 el('text').value='旧草稿';el('text').oninput();const sending=el('send').onclick(),post=p.requests.at(-1);
 el('latest').onclick();const read=p.requests.at(-1);
 p.state(null);assert.equal(el('dialog').open,false);assert.equal(el('text').value,'');
 assert.equal(el('history').children.length,0);assert.equal(el('open').disabled,true);
 p.state('second');el('open').onclick();const fresh=p.requests.at(-1);
 assert.equal(fresh.arguments.session,'second');await p.reply(read,rows);await p.reply(post,{});await sending;
 assert.equal(el('history').children.length,0);assert.equal(el('notice').textContent,'');
 await p.reply(fresh,rows);assert.equal(el('history').children.length,1);
});

test('photos need an explicit send; a failed image-only send retains its nonce and file selection',async()=>{
 const p=page(),el=p.el;
 p.state('owner');el('open').onclick();
 const file={name:'一张图.png',size:3,arrayBuffer:async()=>Buffer.from('abc')};
 el('pick').files=[file];await el('pick').onchange();
 assert.equal(p.requests.length,1);assert.equal(el('send').disabled,false);
 const first=el('send').onclick(),post=p.requests.at(-1);
 assert.equal(post.operation,'room.post_files');assert.equal(post.arguments.text,'');assert.equal(post.files[0],file);
 p.fail(post);await first;assert.equal(el('draft-images').children.length,1);
 const retry=el('send').onclick(),again=p.requests.at(-1);
 assert.equal(again.arguments.nonce,post.arguments.nonce);await p.reply(again,{});await retry;
 assert.equal(el('draft-images').children.length,0);assert.equal(el('send').disabled,true);
});

test('rejects four picks and drops a selection that finishes after lock',async()=>{
 const p=page(),el=p.el;p.state('first');el('open').onclick();
 let release;
 const file={name:'later.png',size:3,arrayBuffer:()=>new Promise(r=>{release=r;})};
 el('pick').files=Array(4).fill(file);await el('pick').onchange();
 assert.equal(el('draft-images').children.length,0);assert.match(el('notice').textContent,/最多 3/);
 el('pick').files=[file];const picking=el('pick').onchange();
 p.state(null);p.state('second');el('open').onclick();
 release(Buffer.from('abc'));await picking;
 assert.equal(el('draft-images').children.length,0);assert.equal(el('send').disabled,true);
});

test('lock clears search and task status and ignores both pending reads',async()=>{
 const p=page(),el=p.el;p.state('owner');el('open').onclick();
 el('query').value='秘密';el('search').onclick();const search=p.requests.at(-1);
 assert.equal(search.operation,'room.search');assert.equal(search.arguments.query,'秘密');
 el('queue').open=true;el('queue').ontoggle();const queue=p.requests.at(-1);
 assert.equal(queue.operation,'room.queue');
 p.state(null);p.state('new');el('open').onclick();
 await p.reply(queue,{messages:[{text:'隐私',status:'processing'}]});
 await p.reply(search,{messages:[{text:'秘密',speaker:'user'}],next:''});
 assert.equal(el('query').value,'');assert.equal(el('jobs').children.length,0);assert.equal(el('history').children.length,0);
});

test('cancel targets only the selected pending reply; a late response cannot reopen a locked room',async()=>{
 const p=page(),el=p.el;p.state('owner');el('open').onclick();
 el('queue').open=true;el('queue').ontoggle();
 await p.reply(p.requests.at(-1),{messages:[{id:'one',text:'先别答',status:'processing'},{id:'two',text:'已提交',status:'dispatching'}]});
 const [first,second]=el('jobs').children,button=first.children[0];
 assert.equal(button.textContent,'取消本次回复');assert.equal(second.children.length,0);
 const click=button.onclick(),cancel=p.requests.at(-1);
 assert.equal(cancel.operation,'room.cancel');assert.equal(cancel.arguments.id,'one');
 assert.equal(button.disabled,true);await button.onclick();assert.equal(p.requests.at(-1),cancel);
 p.state(null);await p.reply(cancel,{cancelled:true});await click;
 assert.equal(el('dialog').open,false);assert.equal(el('notice').textContent,'');
});
