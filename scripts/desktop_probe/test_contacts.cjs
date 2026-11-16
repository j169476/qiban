const test=require('node:test'),assert=require('node:assert/strict');
const page=require('./contacts_test_harness.cjs');
const profiles={accounts:['self'],selected:'self',revision:1};
const rows=Array.from({length:25},(_,i)=>({id:'wxid_'+i,kind:i===24?'group':'private',name:'备注'+i,nickname:'昵称'+i,alias:'Alias'+i}));
const check=(picker,n=0)=>{const box=picker.list.children[n].children[0].children[0];box.checked=true;box.onchange();};
async function wechatSync(p){await p.reply(p.requests.at(-1),profiles);assert.equal(p.requests.at(-1).operation,'wechat.directory_sync');await p.reply(p.requests.at(-1),{items:rows});if(p.requests.at(-1).operation==='wechat.targets.saved')await p.reply(p.requests.at(-1),{version:'v1',targets:[]});}
test('WeChat auto-syncs on entry once; nickname, alias, paging and selection are local',async()=>{
 const p=page('audience.js');p.update(true);assert.equal(p.requests.length,0);await p.enter('wechat');await wechatSync(p);
 const picker=p.picker('audience-contact-picker');assert.equal(picker.list.children.length,10);const count=p.requests.length;
 check(picker);picker.pager.children[2].onclick();assert.equal(picker.list.children.length,10);
 picker.pager.children[3].value='3';picker.pager.children[4].onclick();assert.equal(picker.list.children.length,5);
 for(const query of ['昵称2','ALIAS2']){picker.search.value=query;picker.search.oninput();assert.equal(picker.list.children.length,6);}
 assert.equal(p.requests.length,count);picker.search.value='Alias0';picker.search.oninput();assert.equal(picker.list.children[0].children[0].children[0].checked,true);
 const verify=p.node('audience-verify').onclick();assert.equal(p.requests.at(-1).arguments.targets[0].username,'wxid_0');await p.reply(p.requests.at(-1),{verified:1,unavailable:[]});await verify;
 await p.enter('wechat');assert.equal(p.requests.length,count+1);await p.leave();await p.enter('wechat');assert.equal(p.requests.at(-1).operation,'wechat.profiles');await wechatSync(p);
 assert.equal(picker.list.children[0].children[0].children[0].checked,true);
 const manual=picker.sync.onclick();assert.equal(p.requests.at(-1).operation,'wechat.profiles');await wechatSync(p);await manual;
});
test('WeChat sync arriving after lock is discarded; account change clears the old selection',async()=>{
 const p=page('audience.js');p.update(true);await p.enter('wechat');await p.reply(p.requests.at(-1),profiles);const old=p.requests.at(-1);p.update(false);await p.reply(old,{items:rows});assert.equal(p.picker('audience-contact-picker').list.children.length,0);
 p.update(true);await p.settle();await wechatSync(p);check(p.picker('audience-contact-picker'));await p.leave();await p.enter('wechat');
 await p.reply(p.requests.at(-1),{...profiles,selected:'other',accounts:['other'],revision:2});await p.reply(p.requests.at(-1),{items:[]});await p.reply(p.requests.at(-1),{version:'v2',targets:[]});
 assert.equal(p.node('audience-chosen-list').children.length,0);
});
test('QQ uses the same ten-row picker; entry and manual refresh sync, search never does',async()=>{
 const p=page('qq.js'),state={state:'selected',busy:false,accounts:[],selected:{uin:'10001',label:'测试'},revision:1,targets:{private_targets:['20000'],groups:[],authorized:true},directory:{private_targets:Array.from({length:24},(_,i)=>({id:String(20000+i),label:'备注'+i,nickname:'昵称'+i})),groups:[]}};
 p.update(true);await p.reply(p.requests.at(-1),state);await p.enter('qq');await p.reply(p.requests.at(-1),state);assert.equal(p.requests.at(-1).operation,'qq.directory');await p.reply(p.requests.at(-1),{});await p.reply(p.requests.at(-1),state);
 const picker=p.picker('qq-contact-picker'),count=p.requests.length;assert.equal(picker.list.children.length,10);
 picker.search.value='昵称2';picker.search.oninput();assert.equal(picker.list.children.length,5);check(picker);assert.equal(p.requests.length,count);assert.equal(p.node('qq-private-targets').value,'20000\n20002');
 picker.search.value='';picker.search.oninput();picker.pager.children[3].value='3';picker.pager.children[4].onclick();assert.equal(picker.list.children.length,4);
 await p.enter('qq');assert.equal(p.requests.length,count);await p.leave();await p.enter('qq');await p.reply(p.requests.at(-1),state);assert.equal(p.requests.at(-1).operation,'qq.directory');
 p.update(false);await p.reply(p.requests.at(-1),{});assert.equal(picker.list.children.length,0);
});
