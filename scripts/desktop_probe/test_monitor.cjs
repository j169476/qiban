const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),listeners=[];let sequence=0,activity='idle',generatedAt=null,fail=false,writes=0;
 const create=()=>({dataset:{},children:[],value:'',append(...v){this.children.push(...v)},replaceChildren(){this.children=[]},showModal(){},close(){}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create());return nodes.get(id)};
 node('monitor-window').setActivity=(state,quiet)=>{writes++;node('monitor-window').activity={state,quiet};};
 const webview={addEventListener(_,f){listeners.push(f)},postMessage(m){
  const result=m.operation==='session.status'?{locked:false}:m.operation==='config.read'?{version:'1',data:{name:'',core:{}}}:m.operation==='backups.list'?{backups:[]}:m.operation==='runtime.status'?{generated_at:generatedAt,platforms:[],activities:activity==='idle'?[]:[{state:activity,platform:'qq'}]}:{items:[],unread:0};
  const response=m.operation==='runtime.status'&&fail?{ok:false,message:'连接已中断'}:{ok:true,result};
  queueMicrotask(()=>listeners.forEach(f=>f({data:{type:'management',request_id:m.request_id,response}})));
 }};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/management.js'),'utf8'),{document:{getElementById:node,createElement:create,createTextNode:text=>({textContent:text})},window:{chrome:{webview},dispatchEvent(){},addEventListener(){}},CustomEvent:class{},crypto:{randomUUID:()=>String(++sequence)},setTimeout:()=>0,clearTimeout(){}});
 const settle=()=>new Promise(resolve=>setImmediate(resolve));
 return {node,get writes(){return writes},async ready(value){listeners.forEach(f=>f({data:{type:'service',management:true,ready:value}}));await settle()},async state(value,date=null,offline=false){activity=value;generatedAt=date;fail=offline;await node('runtime-refresh').onclick();await settle()}};
}
test('monitor uses the backend snapshot time and retains it when a refresh fails',async()=>{
 const p=page();await p.ready(true);
 const stamp='2025-02-04T08:17:11.000Z';
 await p.state('replying',stamp);
 const last=p.node('monitor-detail').textContent;
 assert.ok(last.includes(new Date(stamp).toLocaleString('zh-CN',{hour12:false})));
 await p.state('idle',null,true);assert.equal(p.node('monitor-state').textContent,'监控连接中断');
 assert.equal(p.node('monitor-detail').textContent,last);
 assert.deepEqual(p.node('monitor-window').activity,{state:'replying',quiet:true});
 await p.state('watching','bad-date');assert.doesNotMatch(p.node('monitor-detail').textContent,/快照时间|Invalid Date/);
 await p.ready(false);assert.equal(p.node('monitor-detail').textContent,'');
});
test('monitor follows actual activity and switches to a quiet idle frame on disconnect',async()=>{
 const p=page();await p.ready(true);
 for(const state of ['idle','learning','replying','watching','browsing','unknown']){
  await p.state(state);assert.deepEqual(p.node('monitor-window').activity,{state,quiet:false});
 }
 await p.state('replying');await p.ready(false);assert.deepEqual(p.node('monitor-window').activity,{state:'idle',quiet:true});
});
test('monitor and room use eight scheduled pixel backgrounds and seven static poses',()=>{
 const html=fs.readFileSync(path.join(__dirname,'Assets/index.html'),'utf8'),host=fs.readFileSync(path.join(__dirname,'Program.cs'),'utf8');
 assert.match(html,/<qiban-room id="monitor-window"/);assert.match(html,/<qiban-room id="room-window"/);
 assert.match(host,/"\.webp"=>"image\/webp"/);
 const assets=path.join(__dirname,'Assets'), animation=fs.readFileSync(path.join(assets,'room-player.js'),'utf8');
 assert.match(animation,/prefers-reduced-motion: reduce/);
 const context={window:{}};vm.runInNewContext(fs.readFileSync(path.join(assets,'room-assets.js'),'utf8'),context);
 const catalog=context.window.qibanRoomScenes, files=Array.from(catalog.periods,p=>p.src);
 assert.equal(catalog.rendering,'pixel');
 const [width,height]=catalog.size;assert.ok(Number.isInteger(width)&&Number.isInteger(height)&&width*9===height*16);
 const starts=Array.from(catalog.periods,p=>p.start);
 assert.equal(starts.length,8);assert.equal(starts[0],0);
 assert.ok(starts.every((n,i)=>Number.isInteger(n)&&n<1440&&(!i||starts[i-1]<n)));
 assert.deepEqual(Object.keys(catalog.characters.hoodie).sort(),['learning','reading','replying','sleeping','tail','watching','window']);
 for(const pose of Object.values(catalog.characters.hoodie)){
  assert.deepEqual(Object.keys(pose).sort(),['lighting','still']);files.push(pose.still);
  assert.deepEqual(Object.keys(pose.lighting).sort(),Array.from(catalog.periods,p=>p.id).sort());
 }
 assert.equal(new Set(files).size,15);
 assert.match(host,/name\.StartsWith\("room-stills\/"/);
 const project=fs.readFileSync(path.join(__dirname,'Qiban.DesktopProbe.csproj'),'utf8');
 assert.ok(project.includes('Assets/room-stills/*.webp'));assert.ok(!project.includes('Assets/room-art/*.webp'));
 for(const file of files){
  assert.match(file,/^room-stills\/[a-z-]+\.webp$/);
  const bytes=fs.readFileSync(path.join(assets,file));assert.equal(bytes.subarray(8,12).toString(),'WEBP');
 }
});
