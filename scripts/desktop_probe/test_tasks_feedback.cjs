const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),listeners={},sent=[],webListeners=[];
 function node(id){
  if(!nodes.has(id))nodes.set(id,{value:'',disabled:false,textContent:'',children:[],listeners:{},
   append(...children){this.children.push(...children);},replaceChildren(...children){this.children=children;this.textContent='';},
   addEventListener(name,fn){this.listeners[name]=fn;}});
  return nodes.get(id);
 }
 const responses={
  'tasks.list':()=>({items:[],capabilities:{video:{available:true,reason:''}}}),
  'tasks.submit':()=>({id:'task-one'}),
 };
 const webview={addEventListener(name,fn){webListeners.push(fn);},postMessage(request){sent.push(request);queueMicrotask(async()=>{
  try{const result=await responses[request.operation](request);for(const listener of webListeners)listener({data:{type:'management',request_id:request.request_id,response:{ok:true,result}}});}
  catch(error){for(const listener of webListeners)listener({data:{type:'management',request_id:request.request_id,response:{ok:false,message:error.message}}});}
 });}};
 const context={document:{getElementById:node,createElement:()=>node('generated-'+Math.random())},
  window:{chrome:{webview},addEventListener(name,fn){listeners[name]=fn;}},crypto:require('node:crypto').webcrypto,
  setTimeout:()=>1,clearTimeout:()=>{},Map,Number,Error};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/tasks.js'),'utf8'),context);
 const flush=()=>new Promise(resolve=>setImmediate(resolve));
 return {node:id=>node('tasks-'+id),responses,sent,flush,async state(ready=true){listeners['qiban-management-state']({detail:{ready,locked:!ready,roomSession:'owner-session'}});await flush();},fill(){
  for(const [id,value] of Object.entries({bvid:'BV1234567890',page:'1',start:'0',end:'60',mode:'text',reason:'合成阅读要求'}))node('tasks-'+id).value=value;
 }};
}
test('locked entry does not submit; unavailable capability stays disabled',async()=>{
 const p=page();assert.equal(p.node('submit').disabled,true);
 p.responses['tasks.list']=()=>({items:[],capabilities:{video:{available:false,reason:'请先登录'}}});
 await p.state();assert.equal(p.node('submit').disabled,true);assert.equal(p.node('availability').textContent,'请先登录');
});
test('invalid inputs do not reach backend; valid submit shows nearby feedback',async()=>{
 const p=page();await p.state();p.fill();p.node('end').value='0';await p.node('submit').onclick();
 assert.equal(p.sent.filter(x=>x.operation==='tasks.submit').length,0);assert.match(p.node('notice').textContent,/有效/);
 p.node('end').value='60';await p.node('submit').onclick();await p.flush();
 const request=p.sent.find(x=>x.operation==='tasks.submit');assert.equal(request.arguments.arguments.reason,'合成阅读要求');
 assert.equal(request.arguments.session,'owner-session');assert.match(p.node('notice').textContent,/任务已提交/);
});
test('uncertain submit retains nonce; an edit creates a new request identity',async()=>{
 const p=page();await p.state();p.fill();p.responses['tasks.submit']=()=>{throw Error('请求超时');};
 await p.node('submit').onclick();await p.flush();await p.node('submit').onclick();await p.flush();
 const values=p.sent.filter(x=>x.operation==='tasks.submit');assert.equal(values[0].arguments.nonce,values[1].arguments.nonce);
 p.node('reason').listeners.input();await p.node('submit').onclick();await p.flush();
 assert.notEqual(p.sent.filter(x=>x.operation==='tasks.submit')[2].arguments.nonce,values[0].arguments.nonce);
});
test('late results after lock cannot repopulate private task content',async()=>{
 const p=page();let release;p.responses['tasks.list']=()=>new Promise(resolve=>release=resolve);
 await p.state();await p.state(false);release({items:[{arguments:{bvid:'PRIVATE'},status:'completed',result:{summary:'SECRET'}}],capabilities:{video:{available:true}}});
 await p.flush();assert.equal(p.node('items').children.length,0);assert.equal(p.node('submit').disabled,true);
});
test('learning type validates criteria and lock clears the private draft',async()=>{
 const p=page();p.responses['tasks.list']=()=>({items:[],capabilities:{video:{available:false},web:{available:true},learn:{available:true}}});
 await p.state();p.node('kind').value='learn';p.node('kind').listeners.change();p.node('prompt').value='私密学习目标';p.node('criteria').value='apply';p.node('rounds').value='2';
 assert.equal(p.node('video-fields').hidden,true);assert.equal(p.node('learn-fields').hidden,false);
 await p.node('submit').onclick();await p.flush();
 const request=p.sent.find(x=>x.operation==='tasks.submit');assert.equal(request.arguments.kind,'learn');assert.equal(request.arguments.arguments.round_limit,2);
 await p.state(false);assert.equal(p.node('prompt').value,'');assert.equal(p.node('criteria').value,'');
});
test('task output is displayed as text and completed tasks have no retry control',async()=>{
 const p=page();p.responses['tasks.list']=()=>({items:[{id:'task',arguments:{bvid:'BV1234567890'},status:'completed',result:{summary:'<img onerror=evil>'}}],capabilities:{video:{available:true}}});
 await p.state();const row=p.node('items').children[0];assert.equal(row.children[1].textContent,'<img onerror=evil>');
 assert.equal(row.children.length,3);
});
