const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),events={},requests=[],listeners=[];
 const create=()=>({textContent:'',checked:false,children:[],disabled:false,hidden:false,replaceChildren(){this.children=[];},append(...items){this.children.push(...items);}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create());return nodes.get(id);};let sequence=0;
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/growth.js'),'utf8'),{
  document:{getElementById:node,createElement:create},
  window:{chrome:{webview:{addEventListener(_,fn){listeners.push(fn);},postMessage(r){requests.push(r);}}},addEventListener(n,fn){events[n]=fn;}},
  crypto:{randomUUID:()=>String(++sequence)},setTimeout:()=>sequence,clearTimeout(){}
 });
 async function reply(r,value,ok=true){listeners.forEach(fn=>fn({data:{type:'management',request_id:r.request_id,response:ok?{ok:true,result:value}:{ok:false,message:value}}}));await new Promise(resolve=>setImmediate(resolve));}
 return {node,requests,reply,update:allowed=>events['qiban-management-state']({detail:{ready:true,locked:!allowed}})};
}
const proposal={token:'preview',reason:'public_growth_source_changed',data:{mood:'平静🙂',reason:'初始状态',interests:{}}};
async function preview(p){p.update(true);const work=p.node('growth-preview').onclick();await p.reply(p.requests.at(-1),proposal);await work;}
test('healthy inspection does not offer reset or mutate automatically',async()=>{
 const p=page();p.update(true);assert.equal(p.requests.length,0);
 const work=p.node('growth-preview').onclick();await p.reply(p.requests.at(-1),{...proposal,reason:'readable',token:null});await work;
 assert.equal(p.node('growth-proposal').hidden,true);assert.equal(p.node('growth-rebuild').disabled,true);
 p.node('growth-rebuild').onclick();assert.equal(p.requests.length,1);assert.match(p.node('growth-notice').textContent,/无需恢复/);
});
test('preview requires explicit checkbox and cancellation does not write',async()=>{
 const p=page();await preview(p);assert.equal(p.node('growth-rebuild').disabled,true);
 assert.match(p.node('growth-data').children[0].textContent,/心情：平静🙂/);
 p.node('growth-confirm').checked=true;p.node('growth-confirm').onchange();assert.equal(p.node('growth-rebuild').disabled,false);
 p.node('growth-cancel').onclick();assert.equal(p.requests.length,1);assert.equal(p.node('growth-proposal').hidden,true);assert.match(p.node('growth-notice').textContent,/未改变/);
});
test('double submit is suppressed and success preserves stopped state in feedback',async()=>{
 const p=page();await preview(p);p.node('growth-confirm').checked=true;p.node('growth-confirm').onchange();
 const work=p.node('growth-rebuild').onclick();p.node('growth-rebuild').onclick();assert.equal(p.requests.length,2);
 assert.equal(p.requests.at(-1).operation,'growth.rebuild');await p.reply(p.requests.at(-1),{rebuilt:true});await work;
 assert.match(p.node('growth-notice').textContent,/B 站保持停止/);assert.equal(p.node('growth-proposal').hidden,true);
});
test('failed rebuild requires a fresh inspection rather than silent retry',async()=>{
 const p=page();await preview(p);p.node('growth-confirm').checked=true;p.node('growth-confirm').onchange();
 const work=p.node('growth-rebuild').onclick();await p.reply(p.requests.at(-1),'预览已失效',false);await work;
 assert.equal(p.node('growth-rebuild').disabled,true);assert.equal(p.node('growth-preview').disabled,false);assert.match(p.node('growth-notice').textContent,/失效/);
});
test('lock clears old details and rejects delayed responses across unlock',async()=>{
 const p=page();p.update(true);const work=p.node('growth-preview').onclick(),r=p.requests.at(-1);p.update(false);p.update(true);
 await p.reply(r,proposal);await work;assert.equal(p.node('growth-proposal').hidden,true);assert.equal(p.node('growth-data').children.length,0);
});
test('archives are paginated readonly literal text and cleared at lock',async()=>{
 const p=page();p.update(true);let work=p.node('growth-archives').onclick();
 const id='public-growth-recovery:'+'a'.repeat(32);await p.reply(p.requests.at(-1),{items:[id],next:id});await work;
 assert.equal(p.node('growth-archives-next').disabled,false);
 work=p.node('growth-archive-list').children[0].onclick();await p.reply(p.requests.at(-1),{original:{data:{mood:'<img src=x>旧状态🙂'}}});await work;
 assert.match(p.node('growth-archive-content').children[0].textContent,/<img src=x>旧状态🙂/);
 work=p.node('growth-archives-next').onclick();assert.equal(p.requests.at(-1).arguments.after,id);await p.reply(p.requests.at(-1),{items:[],next:null});await work;
 assert.equal(p.node('growth-archive-content').children.length,0);assert.match(p.node('growth-archive-notice').textContent,/没有/);
 p.update(false);assert.equal(p.node('growth-archive-list').children.length,0);assert.equal(p.node('growth-archives').disabled,true);
});
