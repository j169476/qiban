const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),listeners=[];let sequence=0,activity='idle',writes=0;
 const create=()=>({dataset:{},children:[],value:'',append(...v){this.children.push(...v)},replaceChildren(){this.children=[]},showModal(){},close(){}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create());return nodes.get(id)};
 Object.defineProperty(node('monitor-animation'),'src',{set(v){this.url=v;writes++},get(){return this.url}});
 const webview={addEventListener(_,f){listeners.push(f)},postMessage(m){
  const result=m.operation==='session.status'?{locked:false}:m.operation==='config.read'?{version:'1',data:{name:'',core:{}}}:m.operation==='backups.list'?{backups:[]}:m.operation==='runtime.status'?{platforms:[],activities:activity==='idle'?[]:[{state:activity,platform:'qq'}]}:{items:[],unread:0};
  queueMicrotask(()=>listeners.forEach(f=>f({data:{type:'management',request_id:m.request_id,response:{ok:true,result}}})));
 }};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/management.js'),'utf8'),{document:{getElementById:node,createElement:create,createTextNode:text=>({textContent:text})},window:{chrome:{webview},dispatchEvent(){},addEventListener(){}},CustomEvent:class{},crypto:{randomUUID:()=>String(++sequence)},setTimeout:()=>0,clearTimeout(){}});
 const settle=()=>new Promise(resolve=>setImmediate(resolve));
 return {node,get writes(){return writes},async ready(value){listeners.forEach(f=>f({data:{type:'service',management:true,ready:value}}));await settle()},async state(value){activity=value;await node('runtime-refresh').onclick();await settle()}};
}
test('monitor follows runtime activity without restarting an unchanged GIF; disconnection clears it',async()=>{
 const p=page();await p.ready(true);
 for(const [state,asset] of [['idle','idle'],['learning','learning'],['replying','replying'],['watching','watching'],['browsing','watching'],['unknown','idle']]){
  await p.state(state);assert.equal(p.node('monitor-animation').src,`/monitor-${asset}.gif`);assert.equal(p.node('monitor-still').srcset,`/monitor-${asset}.png`);
  const writes=p.writes;await p.state(state);assert.equal(p.writes,writes);
 }
 await p.state('replying');await p.ready(false);assert.equal(p.node('monitor-animation').src,'/monitor-idle.gif');
});
test('reduced motion uses a static picture source; every animation and poster is embedded and routed',()=>{
 const html=fs.readFileSync(path.join(__dirname,'Assets/index.html'),'utf8'),host=fs.readFileSync(path.join(__dirname,'Program.cs'),'utf8');
 assert.match(html,/<source id="monitor-still" media="\(prefers-reduced-motion: reduce\)"/);assert.match(host,/"\.gif"=>"image\/gif"/);
 for(const state of ['idle','learning','watching','replying'])for(const ext of ['gif','png']){
  const name=`monitor-${state}.${ext}`;assert.ok(host.includes(`["/${name}"]="${name}"`));const bytes=fs.readFileSync(path.join(__dirname,'Assets',name));assert.equal(ext==='gif'?bytes.subarray(0,6).toString():bytes.subarray(1,4).toString(),ext==='gif'?'GIF89a':'PNG');
 }
});
