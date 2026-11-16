// QQ management UI tests use only synthetic local replies.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const page=options=>require('./contacts_test_harness.cjs')('audience.js',options);

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

test('account nicknames and WeChat IDs stay bound to stable IDs, including duplicate names and late lock replies',async()=>{
 const p=page({autoDetails:false}),el=id=>p.node('audience-'+id);el('platform').value='wechat';p.update(true);
 const job=el('detect').onclick();
 await p.reply(p.requests.at(-1),{accounts:['one','two'],profiles:{accounts:[],selected:null,revision:0}});
 assert.equal(p.requests.at(-1).arguments.login,'one');
 await p.reply(p.requests.at(-1),{login:'one',nickname:'同名🙂',wechat_id:'visible_id'});
 assert.equal(p.requests.at(-1).arguments.login,'two');
 await p.reply(p.requests.at(-1),{login:'two',nickname:'同名🙂',wechat_id:''});await job;
 assert.equal(el('login').children[0].value,'one');assert.match(el('login').children[0].textContent,/同名🙂.*visible_id/);
 assert.equal(el('login').children[1].value,'two');assert.match(el('login').children[1].textContent,/同名🙂.*two/);
 el('login').value='two';el('login').onchange();assert.equal(el('wechat-id').textContent,'未提供');assert.equal(el('raw-id').textContent,'two');
 const select=el('select').onclick();assert.equal(p.requests.at(-1).arguments.login,'two');
 await p.reply(p.requests.at(-1),{accounts:['two'],selected:'two',revision:1});await select;
 assert.equal(el('accounts').textContent,'当前使用');assert.equal(el('name').textContent,'同名🙂');assert.equal(el('raw-id').textContent,'two');
 const again=el('detect').onclick();await p.reply(p.requests.at(-1),{accounts:['one'],profiles:{accounts:[],selected:null,revision:1}});
 const late=p.requests.at(-1);p.update(false);await p.reply(late,{login:'one',nickname:'must not appear',wechat_id:'private-id'});await again;
 assert.equal(el('login').children.length,0);assert.equal(el('accounts').textContent,'');
});

test('mismatched account details never relabel another account',async()=>{
 const p=page({autoDetails:false}),el=id=>p.node('audience-'+id);el('platform').value='wechat';p.update(true);
 const job=el('detect').onclick();await p.reply(p.requests.at(-1),{accounts:['one'],profiles:{accounts:[],selected:null,revision:0}});
 await p.reply(p.requests.at(-1),{login:'two',nickname:'wrong-person',wechat_id:'wrong-id'});await job;
 assert.equal(el('login').children[0].textContent,'one（已登录）');assert.match(el('account-notice').textContent,/暂未读取成功/);
});
