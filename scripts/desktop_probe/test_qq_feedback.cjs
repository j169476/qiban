// QQ management UI tests use only synthetic local replies.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const page=options=>require('./contacts_test_harness.cjs')('qq.js',options);

const idle={state:'idle',busy:false,accounts:[],selected:null,revision:0};
const qr={...idle,state:'qr_ready',busy:true,png_base64:'synthetic',message:'请扫码'};

test('password mode submits explicitly and clears the field, switching and locking also clear it',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],idle);
 p.node('qq-login-method').value='password';p.node('qq-login-method').onchange();
 assert.equal(p.node('qq-password-fields').hidden,false);
 p.node('qq-login-account').value='10001';p.node('qq-login-password').value='synthetic-password';
 const action=p.node('qq-login').onclick(),request=p.requests.at(-1);
 assert.equal(request.operation,'qq.password');assert.equal(request.arguments.account,'10001');
 assert.equal(request.arguments.password,'synthetic-password');assert.equal(p.node('qq-login-password').value,'');
 await p.reply(request,{started:true});await p.reply(p.requests.at(-1),{...idle,busy:true,state:'password_pending'});await action;
 assert.equal(request.arguments.password,'');assert.equal(p.node('qq-login-method').disabled,true);
 p.update(false);assert.equal(p.node('qq-login-password').value,'');
 p.update(true);await p.reply(p.requests.at(-1),idle);
 p.node('qq-login-password').value='another-password';p.node('qq-login-method').value='qr';p.node('qq-login-method').onchange();
 assert.equal(p.node('qq-login-password').value,'');assert.equal(p.node('qq-password-fields').hidden,true);
});

test('opening only checks status; lock clears QR and discards late responses',async()=>{
 const p=page();p.update(true);assert.equal(p.requests[0].operation,'qq.status');
 await p.reply(p.requests[0],qr);assert.equal(p.node('qq-qr').src,'data:image/png;base64,synthetic');
 p.tick();const late=p.requests.at(-1);p.update(false);
 assert.equal(p.node('qq-qr').src,undefined);assert.equal(p.node('qq-qr-area').hidden,true);
 await p.reply(late,qr);assert.equal(p.node('qq-qr').src,undefined);
 assert.ok(p.requests.every(r=>r.operation==='qq.status'));
});
test('login during pending status queues a fresh read and continues QR polling',async()=>{
 const p=page();p.update(true);const initial=p.requests[0];
 const action=p.node('qq-login').onclick();const login=p.requests.at(-1);
 assert.equal(login.operation,'qq.login');await p.reply(login,{started:true});await action;
 await p.reply(initial,idle);assert.equal(p.requests.at(-1).operation,'qq.status');
 await p.reply(p.requests.at(-1),qr);assert.equal(p.node('qq-qr-area').hidden,false);
 const count=p.requests.length;p.tick();assert.equal(p.requests.length,count+1);
});
test('cancel completion removes QR and leaves no selectable account',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],qr);
 const action=p.node('qq-cancel').onclick();await p.reply(p.requests.at(-1),{cancel_requested:true});
 await p.reply(p.requests.at(-1),{...idle,state:'cancelled',message:'QQ 登录操作已取消。'});await action;
 assert.equal(p.node('qq-qr').src,undefined);assert.equal(p.node('qq-select').disabled,true);
 assert.match(p.node('qq-status').textContent,/已取消/);
});
test('saving requires explicit action and sends only verified UIN and revision',async()=>{
 const p=page(),account={uin:'10001',uid:'u_test',label:'测试🙂',can_quick_login:true};
 p.update(true);await p.reply(p.requests[0],{...idle,state:'available',accounts:[account],revision:4});
 assert.equal(p.requests.length,1);assert.equal(p.node('qq-selection').value,'10001');
 const action=p.node('qq-select').onclick(),request=p.requests.at(-1);
 assert.equal(JSON.stringify(request.arguments),JSON.stringify({account:'10001',expected:4}));
 await p.reply(request,{});await p.reply(p.requests.at(-1),{...idle,state:'selected',selected:account,revision:5});await action;
 assert.match(p.node('qq-notice').textContent,/已保存/);assert.match(p.node('qq-current').textContent,/测试🙂/);
});
test('late save feedback cannot leak across a management lock and unlock',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{...idle,accounts:[{uin:'10001',label:'测试',can_quick_login:true}]});
 const action=p.node('qq-select').onclick(),save=p.requests.at(-1);p.update(false);p.update(true);
 await p.reply(save,{});await action;assert.equal(p.node('qq-notice').textContent,'');
});

test('target preview cannot authorize edited numbers and lock clears the editor',async()=>{
 const p=page(),account={uin:'10001',uid:'u_test',label:'测试',can_quick_login:true};
 p.update(true);await p.reply(p.requests[0],{...idle,selected:account,revision:1});
 p.node('qq-private-targets').value='20001，20002';p.node('qq-group-targets').value='30001';
 const action=p.node('qq-targets-verify').onclick(),request=p.requests.at(-1);
 assert.equal(request.operation,'qq.targets.verify');
 assert.equal(JSON.stringify(request.arguments),JSON.stringify({private_targets:['20001','20002'],groups:['30001'],expected:1}));
 await p.reply(request,{});await p.reply(p.requests.at(-1),{...idle,selected:account,revision:1,
  preview:{token:'verified-token',private_targets:['20001','20002'],groups:['30001']}});await action;
 assert.equal(p.node('qq-targets-save').disabled,false);
 p.node('qq-private-targets').value='20003';p.node('qq-private-targets').oninput();
 assert.equal(p.node('qq-targets-save').disabled,true);
 p.update(false);assert.equal(p.node('qq-private-targets').value,'');assert.equal(p.node('qq-targets-preview').textContent,'');
 assert.equal(p.node('qq-targets-area').hidden,true);
});

test('persisted target settings load without granting or checking automatically',async()=>{
 const p=page();p.update(true);await p.reply(p.requests[0],{...idle,selected:{uin:'10001',label:'测试'},revision:5,
  targets:{private_targets:['20001'],groups:['30001'],authorized:false}});
 assert.equal(p.node('qq-private-targets').value,'20001');assert.equal(p.node('qq-group-targets').value,'30001');
 assert.match(p.node('qq-targets-summary').textContent,/授权已关闭/);
 assert.equal(p.node('qq-targets-save').disabled,true);assert.equal(p.requests.length,1);
});
