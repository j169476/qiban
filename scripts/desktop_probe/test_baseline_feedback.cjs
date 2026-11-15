const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),events={},requests=[],listeners=[];let sequence=0;
 const create=tag=>({tag,textContent:'',value:'',checked:false,children:[],disabled:false,hidden:false,
  replaceChildren(){this.children=[];},append(...items){this.children.push(...items);},
  querySelectorAll(tag){return this.children.flatMap(c=>[...(c.tag===tag?[c]:[]),...c.querySelectorAll(tag)]);}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create('div'));return nodes.get(id);};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/baseline.js'),'utf8'),{
  document:{getElementById:node,createElement:create},window:{chrome:{webview:{addEventListener(_,f){listeners.push(f);},postMessage(r){requests.push(r);}}},addEventListener(n,f){events[n]=f;}},
  crypto:{randomUUID:()=>String(++sequence)},setTimeout:()=>sequence,clearTimeout(){}
 });
 async function reply(value,ok=true,r=requests.at(-1)){listeners.forEach(f=>f({data:{type:'management',request_id:r.request_id,response:ok?{ok:true,result:value}:{ok:false,message:value}}}));await new Promise(r=>setImmediate(r));}
 return {node,requests,reply,update:allowed=>events['qiban-management-state']({detail:{ready:true,locked:!allowed}})};
}
const card={version:'v1',policy:{version:'p1'},data:{id:'main',name:'合成人物',core:{description:'<img src=x> 描述',speaking_style:['自然']},interests:[],locked_fields:['core'],foundation_memories:[]}};
const preview=(shared=false)=>({shared,foundation_count:0,pins:[{kind:'persona',id:null,version:'v1',policy_version:'p1'}]});
async function load(p,shared=false){p.update(true);await p.reply(preview(shared));await p.reply(card);}
test('publication requires complete preview and explicit confirmation; heartbeat never submits',async()=>{
 const p=page();await load(p);const count=p.requests.length;
 p.node('baseline-publish').onclick();for(let i=0;i<4;i++)p.update(true);
 assert.equal(p.requests.length,count);assert.equal(p.node('baseline-publish').disabled,true);
 assert.match(p.node('baseline-records').children[0].children[1].textContent,/<img src=x>/);
 p.node('baseline-confirm').checked=true;p.node('baseline-confirm').onchange();
 const work=p.node('baseline-publish').onclick();assert.equal(p.requests.at(-1).operation,'baseline.publish');
 assert.equal(p.requests.at(-1).arguments.expected.pins[0].policy_version,'p1');
 await p.reply({shared:true});await p.reply(preview(true));await p.reply(card);await work;
 assert.match(p.node('baseline-notice').textContent,/共用设定已启用/);assert.equal(p.node('baseline-publish-area').hidden,true);
});
test('stale detail cannot be confirmed or published',async()=>{
 const p=page();p.update(true);await p.reply(preview());await p.reply({...card,version:'changed'});
 assert.match(p.node('baseline-notice').textContent,/发生变化/);assert.equal(p.node('baseline-publish').disabled,true);
});
test('failed foundation save preserves draft and blocks navigation until discard',async()=>{
 const p=page();await load(p,true);
 p.node('foundation-content').value='新的合成基础记忆🙂';p.node('foundation-content').oninput();
 p.node('foundation-confirm').checked=true;p.node('foundation-confirm').onchange();
 const work=p.node('foundation-add').onclick();assert.equal(p.requests.at(-1).operation,'foundation.add');
 assert.equal(p.requests.at(-1).arguments.expected,'v1');await p.reply('人物卡已修改',false);await work;
 p.update(true);assert.equal(p.node('foundation-content').value,'新的合成基础记忆🙂');assert.equal(p.node('baseline-refresh').disabled,true);
 p.node('foundation-discard').onclick();assert.equal(p.node('foundation-content').value,'');assert.equal(p.node('baseline-refresh').disabled,false);
});
test('locking clears records and rejects late replies',async()=>{
 const p=page();p.update(true);const request=p.requests.at(-1);p.update(false);await p.reply(preview(),true,request);
 assert.equal(p.requests.length,1);assert.equal(p.node('baseline-records').children.length,0);assert.equal(p.node('baseline-publish').disabled,true);
});
test('successful write followed by failed reload is reported as saved, not retried',async()=>{
 const p=page();await load(p);p.node('baseline-confirm').checked=true;
 const work=p.node('baseline-publish').onclick();await p.reply({shared:true});await p.reply('读取失败',false);await work;
 assert.match(p.node('baseline-notice').textContent,/已启用.*列表暂未更新/);
 assert.equal(p.requests.filter(r=>r.operation==='baseline.publish').length,1);
 assert.equal(p.node('baseline-publish').disabled,true);
});
test('removal changes only foundation list and never calls delete',async()=>{
 const p=page();p.update(true);
 await p.reply({shared:true,foundation_count:1,pins:[...preview().pins,{kind:'memories',id:'one',version:'m1',policy_version:'p2'}]});
 await p.reply({...card,data:{...card.data,foundation_memories:['one']}});
 await p.reply({version:'m1',policy:{version:'p2'},data:{id:'one',content:'合成公开设定'}});
 const work=p.node('baseline-records').querySelectorAll('button')[0].onclick();
 assert.equal(p.requests.at(-1).operation,'config.patch');assert.equal(p.requests.at(-1).arguments.changes.foundation_memories.length,0);
 await p.reply({version:'v2'});await p.reply(preview(true));await p.reply(card);await work;
 assert.equal(p.requests.some(r=>r.operation.includes('delete')),false);assert.match(p.node('baseline-notice').textContent,/原记录与历史保留/);
});

test('shared protection is explicit even when an inherited memory has locked=false',async()=>{
 const p=page();p.update(true);
 await p.reply({shared:true,foundation_count:1,pins:[...preview().pins,{kind:'memories',id:'one',version:'m1',policy_version:'p2'}]});
 await p.reply({...card,data:{...card.data,foundation_memories:['one']}});
 await p.reply({version:'m1',policy:{version:'p2'},data:{id:'one',content:'explicit',kind:'user_preference',confidence:'explicit',locked:false,source:{type:'user_instruction'}}});
 const rows=p.node('baseline-records').children[1].children;
 assert.match(rows[1].textContent,/单条记忆锁定标记：否/);
 assert.match(rows[1].textContent,/可信程度：明确告知/);
 assert.match(rows[1].textContent,/正文：explicit/);
 assert.match(rows[2].textContent,/只能由管理员修改/);
 assert.match(rows[2].textContent,/只能由管理员修改/);
 assert.equal(p.requests.some(r=>r.operation==='records.correct'),false);
});
