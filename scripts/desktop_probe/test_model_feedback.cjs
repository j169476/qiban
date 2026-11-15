// Browser-controller contract tests with synthetic account replies only.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

function page(initial){
 const nodes=new Map(),events={},messages=[];
 const node=id=>{
  if(!nodes.has(id))nodes.set(id,{textContent:'',disabled:false,hidden:false,open:false,value:'',children:[],
   replaceChildren(){this.children=[];this.value='';},append(child){this.children.push(child);},
   showModal(){this.open=true;},close(){this.open=false;}});
  return nodes.get(id);
 };
 let state=initial,clock=10000,sequence=0;
 const responses={status:()=>state,refresh:()=>{state={state:'signed_out',busy:false,message:'尚未登录，请使用 ChatGPT 账号登录。'};return {};},
  cancel:()=>({cancel_requested:true}),logout:()=>{state={state:'working',busy:true,message:'正在连接 Codex……'};return {};}};
 const webview={addEventListener(name,fn){messages.push(fn);},postMessage(request){
  queueMicrotask(()=>{
   const operation=request.operation.slice('model.'.length),result=responses[operation](request.arguments);
   for(const fn of messages)fn({data:{type:'management',request_id:request.request_id,response:{ok:true,result}}});
  });
 }};
 const context={document:{getElementById:node,createElement:()=>({value:'',textContent:''})},window:{chrome:{webview},addEventListener(name,fn){events[name]=fn;}},
  crypto:{randomUUID:()=>String(++sequence).padStart(32,'0')},Date:{now:()=>clock},setTimeout,clearTimeout};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'assets/model.js'),'utf8'),context);
 const settle=()=>new Promise(resolve=>setImmediate(resolve));
 async function update(value,allowed=true){if(value)state=value;clock+=2000;events['qiban-management-state']({detail:{ready:true,locked:!allowed}});await settle();}
 return {node,update,settle,responses};
}

test('interrupted handoff can resume with the only saved current account after reopening',async()=>{
 const state={state:'unchecked',busy:false,multi_account:true,active_account:'a',accounts_revision:7,
  handoff_pending:true,accounts:[{id:'a',label:'账号 1',active:true,signed_in:true}]};
 const p=page(state);await p.update();
 assert.equal(p.node('model-select').disabled,false);
 assert.match(p.node('model-status').textContent,/交接未完成/);
 let requested;
 p.responses.select=args=>{requested=args;return {};};
 await p.node('model-select').onclick();
 assert.equal(requested.account_id,'a');assert.equal(requested.expected,7);
 await p.update({...state,busy:true});
 assert.equal(p.node('model-select').disabled,true);
 await p.update({...state,state:'authenticated',handoff_pending:false});
 assert.equal(p.node('model-select').disabled,true);
 assert.equal(p.node('model-feedback-title').textContent,'当前账号已切换');
 await p.update(state,false);
 assert.equal(p.node('model-select').disabled,true);
});

test('cancellation waits for completion and feedback survives a subsequent status check',async()=>{
 const p=page({state:'waiting',busy:true,can_open_login:true,message:'等待授权'});
 await p.update();await p.node('model-cancel').onclick();
 assert.equal(p.node('model-feedback').open,false);
 assert.equal(p.node('model-notice').textContent,'正在取消账号操作，请稍候……');
 await p.update({state:'cancelled',busy:false,message:'已取消账号操作；请重新检查当前登录状态。'});
 assert.equal(p.node('model-feedback-title').textContent,'账号操作已取消');
 assert.equal(p.node('model-feedback').open,true);
 p.node('model-feedback-close').onclick();
 await p.node('model-refresh').onclick();
 assert.equal(p.node('model-feedback').open,false);
 assert.equal(p.node('model-notice').textContent,'已取消账号操作；请重新检查当前登录状态。');
});

test('logout displays completion only after backend confirms signed out',async()=>{
 const p=page({state:'authenticated',busy:false,email:'synthetic@example.invalid',plan:'plus'});
 await p.update();await p.node('model-logout').onclick();
 assert.equal(p.node('model-feedback').open,false);
 await p.update({state:'signed_out',busy:false,message:'尚未登录'});
 assert.equal(p.node('model-account').textContent,'');
 assert.equal(p.node('model-feedback-title').textContent,'已退出本应用账号');
 assert.equal(p.node('model-feedback').open,true);
});

test('already completed login cannot be reported as successfully cancelled',async()=>{
 const p=page({state:'waiting',busy:true,can_open_login:true,message:'等待授权'});
 await p.update();await p.node('model-cancel').onclick();
 await p.update({state:'authenticated',busy:false,email:'synthetic@example.invalid',plan:'plus'});
 assert.equal(p.node('model-feedback-title').textContent,'登录已经完成');
 assert.match(p.node('model-account').textContent,/synthetic@example.invalid/);
});

test('locking management clears pending account feedback without reopening a modal',async()=>{
 const p=page({state:'waiting',busy:true,can_open_login:true,message:'等待授权'});
 await p.update();await p.node('model-cancel').onclick();
 await p.update(null,false);
 await p.update({state:'cancelled',busy:false,message:'已取消'},true);
 assert.equal(p.node('model-feedback').open,false);
 assert.equal(p.node('model-notice').textContent,'');
});

test('SIWC logout preserves the remote revocation warning',async()=>{
 const p=page({state:'authenticated',busy:false,authentication_route:'siwc_validation'});
 await p.update();assert.equal(p.node('model-login').textContent,'继续 ChatGPT 登录');
 await p.node('model-logout').onclick();
 await p.update({state:'signed_out',busy:false,remote_revocation_confirmed:false,message:'远端撤销未确认'});
 assert.equal(p.node('model-feedback-message').textContent,'远端撤销未确认');
});

test('late cancellation does not misreport a retained rotating credential',async()=>{
 const p=page({state:'working',busy:true,message:'正在续期'});
 await p.update();await p.node('model-cancel').onclick();
 await p.update({state:'authenticated',busy:false,renewal_status:'complete',message:'授权续期完成'});
 assert.equal(p.node('model-feedback-title').textContent,'续期操作结果');
 assert.equal(p.node('model-feedback-message').textContent,'授权续期完成');
});

test('account dropdown requires explicit selection and keeps selection during polling',async()=>{
 const first={state:'authenticated',busy:false,multi_account:true,active_account:'a',accounts_revision:4,
  accounts:[{id:'a',label:'账号 1 · same',active:true,signed_in:true},{id:'b',label:'账号 2 · same',active:false,signed_in:true}]};
 const p=page(first);await p.update();
 assert.equal(p.node('model-selection').value,'a');
 assert.equal(p.node('model-select').disabled,true);
 p.node('model-selection').value='b';p.node('model-selection').onchange();
 await p.update(first);
 assert.equal(p.node('model-selection').value,'b');
 let selected;
 p.responses.select=args=>{selected=args;return {};};
 await p.node('model-select').onclick();
 assert.equal(selected.account_id,'b');assert.equal(selected.expected,4);
 await p.update({...first,active_account:'b',accounts_revision:5,
  accounts:first.accounts.map(a=>({...a,active:a.id==='b'}))});
 assert.equal(p.node('model-feedback-title').textContent,'当前账号已切换');
 await p.update(null,false);
 assert.equal(p.node('model-selection').children.length,0);
 assert.equal(p.node('model-accounts').hidden,true);
});

test('successful addition selects the new active account in the dropdown',async()=>{
 const p=page({state:'authenticated',busy:false,multi_account:true,active_account:'a',accounts_revision:1,
  accounts:[{id:'a',label:'账号 1',active:true,signed_in:true}]});
 await p.update();
 await p.update({state:'authenticated',busy:false,multi_account:true,active_account:'b',accounts_revision:2,
  accounts:[{id:'a',label:'账号 1',active:false,signed_in:true},{id:'b',label:'账号 2',active:true,signed_in:true}]});
 assert.equal(p.node('model-selection').value,'b');
 assert.match(p.node('model-account').textContent,/账号 2/);
});
