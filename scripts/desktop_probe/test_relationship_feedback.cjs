const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),events={},requests=[],listeners=[];let sequence=0;
 const create=tag=>({tag,textContent:'',value:'',checked:false,children:[],disabled:false,hidden:false,
  replaceChildren(){this.children=[];},append(...items){this.children.push(...items);},
  querySelectorAll(tag){return this.children.flatMap(c=>[...(c.tag===tag?[c]:[]),...c.querySelectorAll(tag)]);}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create('div'));return nodes.get(id);};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/relationship.js'),'utf8'),{
  document:{getElementById:node,createElement:create,createTextNode:text=>({...create("text"),textContent:text})},window:{chrome:{webview:{addEventListener(_,f){listeners.push(f);},postMessage(r){requests.push(r);}}},addEventListener(n,f){events[n]=f;}},
  crypto:{randomUUID:()=>String(++sequence)},setTimeout:()=>sequence,clearTimeout(){}
 });
 async function reply(value,ok=true,r=requests.at(-1)){listeners.forEach(f=>f({data:{type:'management',request_id:r.request_id,response:ok?{ok:true,result:value}:{ok:false,message:value}}}));await new Promise(r=>setImmediate(r));}
 return {node,requests,reply,update:allowed=>events['qiban-management-state']({detail:{ready:true,locked:!allowed}})};
}
const qq={key:'one',platform:'qq',user_id:'123',scope:'bot'},wx={key:'two',platform:'wechat',user_id:'abc',scope:'test'};
const base={expected:'v1',shared:true,setting:{target:'person',owner_name:'测试角色',level:'朋友',addresses:['伙伴🙂'],description:'开发资料'},anchor:'one',selected:['one']};
async function open(p){
 p.update(true);assert.equal(p.requests.length,0);const job=p.node('relation-read').onclick();
 await p.reply(base);await p.reply({accounts:[qq,wx],next:null});await p.reply({members:[qq,wx]});await job;
}
test('explicit selection, stale edit preserves draft, single retry saves only checked accounts',async()=>{
 const p=page();await open(p);const [main,other]=p.node('relation-members').querySelectorAll('input');
 assert.equal(main.disabled,true);assert.equal(other.checked,false);other.checked=true;other.onchange();
 p.update(true);assert.equal(other.checked,true);let job=p.node('relation-save').onclick();
 assert.deepEqual(Array.from(p.requests.at(-1).arguments.selected),['one','two']);
 assert.equal(p.requests.at(-1).arguments.expected,'v1');await p.reply('版本冲突',false);await job;
 assert.equal(other.checked,true);assert.match(p.node('relation-notice').textContent,/版本冲突/);
 other.checked=false;other.onchange();job=p.node('relation-save').onclick();assert.deepEqual(Array.from(p.requests.at(-1).arguments.selected),['one']);
 await p.reply({version:'v2',selected_count:1});await job;assert.match(p.node('relation-notice').textContent,/适用于1个/);assert.equal(p.node('relation-save').disabled,true);
});
test('cancel unlink has no side effect; confirmed unlink uses saved revision',async()=>{
 const p=page();await open(p);const n=p.requests.length;p.node('relation-clear').onclick();p.node('relation-clear-cancel').onclick();assert.equal(p.requests.length,n);
 p.node('relation-clear').onclick();const job=p.node('relation-clear-confirm').onclick();assert.equal(p.requests.at(-1).operation,'relationship.clear');assert.equal(p.requests.at(-1).arguments.expected,'v1');
 await p.reply({version:'v2'});await job;assert.match(p.node('relation-notice').textContent,/历史保留/);
});
test('late account-picker response cannot restore data after locking',async()=>{
 const p=page();p.update(true);const job=p.node('relation-read').onclick();await p.reply(base);const outstanding=p.requests.at(-1);p.update(false);await p.reply({accounts:[qq],next:null},true,outstanding);await job;
 assert.equal(p.requests.length,2);assert.equal(p.node('relation-form').hidden,true);assert.equal(p.node('relation-name').value,'');
});
const linkRow={id:'link-one-two',version:'lv1',status:'hard',status_label:'已确认同一人',manual:true,locked:true,revocable:true,left:qq,right:wx};
const linkView={id:'link-one-two',version:'lv1',status:'hard',status_label:'已确认同一人',stored_status:'hard',manual:true,locked:true,
 left:qq,right:wx,reason:'合成原因',
 evidence:{items:[{event_id:'event-1',stored_version:'event-v1',current_version:'event-v1',state:'stored',
   state_label:'当时保存的引用原文（保存的版本与当前一致）',quote:'约在周五看蓝色草图。'}],count:1,limit:30,text_limit:12000,meaning:'引用说明'},
 history:{items:[{at:'2026-01-01T00:00:00Z',status:'hard',status_label:'已确认同一人',kind:'manual_confirmation',
   reason:'本机管理员已核对两个平台账号并确认是同一人',evidence_count:0}],count:1,limit:100,truncated:false,meaning:'历史说明'},
 person:{accounts:[qq,wx],count:2,inferred:true,person_id:'person-1',grants_permissions:false,notice:'成员说明'},
 revocable:true,reason_max:1000,notice:'确认后只撤销这一条账号关联…',meaning:'通用说明'};
async function openLink(p,view=linkView){
 const job=p.node('relation-link-list').onclick();
 await p.reply({links:[linkRow],count:1,total:1,page_max:50,after:'',next:null,meaning:'列表说明'});await job;
 const item=p.node('relation-link-items').children[0];assert.match(item.textContent,/已确认同一人/);
 const detailJob=item.onclick();await p.reply(view);await detailJob;
}
test('existing link detail shows stored quote, members and recent history',async()=>{
 const p=page();await open(p);await openLink(p);
 assert.equal(p.requests.at(-1).operation,'relationship.link_detail');
 assert.equal(p.requests.at(-1).arguments.identifier,'link-one-two');
 const text=p.node('relation-link-detail').children.map(node=>node.textContent).join('\n');
 assert.match(text,/约在周五看蓝色草图。/);assert.match(text,/当时保存的引用原文/);assert.match(text,/共2个/);
 assert.match(text,/最近100条/);assert.equal(p.node('relation-link-revoke-area').hidden,false);
 assert.equal(p.node('relation-link-revoke').disabled,false);
});
test('cancelling the revocation confirmation writes nothing',async()=>{
 const p=page();await open(p);await openLink(p);const counted=p.requests.length;
 p.node('relation-link-revoke-cancel').onclick();
 assert.equal(p.requests.length,counted);assert.equal(p.node('relation-link-revoke-area').hidden,true);
 assert.equal(p.node('relation-link-detail').children.length,0);assert.match(p.node('relation-notice').textContent,/没有写入/);
});
test('confirmed revocation sends the read version once and refreshes list, detail and members',async()=>{
 const p=page();await open(p);await openLink(p);p.node('relation-link-revoke-reason').value='误判，不是同一人';
 const job=p.node('relation-link-revoke').onclick();
 const sent=p.requests.at(-1);
 assert.equal(sent.operation,'relationship.link_revoke');
 assert.equal(sent.arguments.identifier,'link-one-two');
 assert.equal(sent.arguments.expected,'lv1');
 assert.equal(sent.arguments.reason,'误判，不是同一人');
 assert.deepEqual(Object.keys(sent.arguments).sort().join(','),'expected,identifier,reason');
 assert.equal(p.node('relation-link-revoke').disabled,true);
 const writes=()=>p.requests.filter(r=>r.operation==='relationship.link_revoke').length;
 p.node('relation-link-revoke').onclick();assert.equal(writes(),1);
 await p.reply({revoked:true,version:'lv2'});
 assert.equal(p.requests.at(-1).operation,'relationship.links');
 await p.reply({links:[{...linkRow,status:'revoked',status_label:'已撤销',version:'lv2',revocable:false}],count:1,total:1,page_max:50,after:'',next:null,meaning:'列表说明'});
 assert.equal(p.requests.at(-1).operation,'relationship.link_detail');
 await p.reply({...linkView,version:'lv2',status:'revoked',stored_status:'revoked',status_label:'已撤销',revocable:false});
 assert.equal(p.requests.at(-1).operation,'relationship.person');
 await p.reply({members:[qq]});
 await job;
 assert.equal(writes(),1);
 assert.match(p.node('relation-notice').textContent,/已撤销这条账号关联/);
 assert.match(p.node('relation-notice').textContent,/请重新核对适用账号/);
 assert.equal(p.node('relation-link-revoke-area').hidden,true);
 assert.equal(p.node('relation-link-revoke').disabled,true);
 assert.match(p.node('relation-link-list-status').textContent,/共1条/);
});
test('a failed revocation is not retried and re-reads the current link',async()=>{
 const p=page();await open(p);await openLink(p);const job=p.node('relation-link-revoke').onclick();
 await p.reply('这条账号关联已变化，请重新核对',false);
 assert.equal(p.requests.at(-1).operation,'relationship.link_detail');
 await p.reply({...linkView,version:'lv9'});await job;
 assert.equal(p.requests.filter(r=>r.operation==='relationship.link_revoke').length,1);
 assert.match(p.node('relation-notice').textContent,/不会自动重试/);
 assert.equal(p.node('relation-link-revoke-area').hidden,false);
});
test('a late revocation result cannot revive the cleared page after locking',async()=>{
 const p=page();await open(p);await openLink(p);const job=p.node('relation-link-revoke').onclick();
 const outstanding=p.requests.at(-1);const counted=p.requests.length;
 p.update(false);await p.reply({revoked:true,version:'lv2'},true,outstanding);await job;
 assert.equal(p.requests.length,counted);
 assert.equal(p.node('relation-link-items').children.length,0);
 assert.equal(p.node('relation-link-detail').children.length,0);
 assert.equal(p.node('relation-link-revoke-area').hidden,true);
 assert.equal(p.node('relation-notice').textContent,'');
});
