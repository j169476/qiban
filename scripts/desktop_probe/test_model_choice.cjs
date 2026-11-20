const test=require('node:test'),assert=require('node:assert/strict');
const page=require('./contacts_test_harness.cjs');
const value={account_id:'a',revision:'1',model:'first',effort:'low',available:true,models:[{id:'first',name:'First',efforts:['low','high'],default_effort:'low'},{id:'second',name:'Second',efforts:['medium','ultra'],default_effort:'medium'}]};
test('Ultrafast follows the selected model capability and rolls back failed changes',async()=>{
 const p=page('model-choice.js');p.body.dataset.navPage='model';p.update(true);
 const catalog={...value,ultrafast:true,models:value.models.map((m,i)=>({...m,ultrafast:i===0}))};
 await p.reply(p.requests.at(-1),catalog);
 const toggle=p.node('model-ultrafast');
 assert.equal(toggle.hidden,false);assert.equal(toggle['aria-pressed'],'true');
 toggle.onclick();await p.advance(350);assert.equal(p.requests.at(-1).arguments.ultrafast,false);
 await p.fail(p.requests.at(-1));assert.equal(toggle['aria-pressed'],'true');
 p.node('model-choice').value='second';p.node('model-choice').onchange();
 assert.equal(toggle.hidden,true);await p.advance(350);assert.equal(p.requests.at(-1).arguments.ultrafast,false);
 await p.reply(p.requests.at(-1),{...catalog,model:'second',ultrafast:false,effort:'medium'});
 p.node('model-choice').value='first';p.node('model-choice').onchange();assert.equal(toggle['aria-pressed'],'false');
 p.update(false);assert.equal(toggle.hidden,true);
});
test('provider-managed reasoning has no selectable levels and saves null once',async()=>{
 const p=page('model-choice.js');p.body.dataset.navPage='model';p.update(true);
 const models=[...value.models,{id:'auto',name:'Auto',efforts:[],default_effort:null}];
 await p.reply(p.requests.at(-1),{...value,models});
 p.node('model-choice').value='auto';p.node('model-choice').onchange();
 assert.equal(p.node('model-effort').disabled,true);assert.equal(p.node('model-effort').children[0].textContent,'由模型决定');
 assert.equal(p.node('model-choice-save').disabled,false);
 const saving=p.node('model-choice-save').onclick(),req=p.requests.at(-1);
 assert.deepEqual(JSON.parse(JSON.stringify(req.arguments)),{account_id:'a',expected:'1',model:'auto',effort:null});
 await p.reply(req,{...value,models,model:'auto',effort:null,revision:'2'});await saving;await p.advance(350);
 assert.equal(p.requests.filter(r=>r.operation==='model.configure').length,1);
 p.node('model-choice').value='first';p.node('model-choice').onchange();assert.equal(p.node('model-effort').disabled,false);
});
test('model and reasoning choices follow upstream; explicit save sends the selected pair',async()=>{
 const p=page('model-choice.js');p.body.dataset.navPage='model';p.update(true);assert.equal(p.requests.at(-1).operation,'model.catalog');await p.reply(p.requests.at(-1),value);
 assert.equal(p.node('model-effort').value,'low');p.node('model-choice').value='second';p.node('model-choice').onchange();
 assert.deepEqual(p.node('model-effort').children.map(o=>o.value),['medium','ultra']);assert.equal(p.node('model-effort').value,'medium');
 p.node('model-effort').value='ultra';const save=p.node('model-choice-save').onclick();assert.deepEqual(JSON.parse(JSON.stringify(p.requests.at(-1).arguments)),{account_id:'a',expected:'1',model:'second',effort:'ultra'});
 await p.reply(p.requests.at(-1),{...value,model:'second',effort:'ultra',revision:'2'});await save;assert.match(p.node('model-choice-status').textContent,/已保存/);
 const refresh=p.node('model-catalog-refresh').onclick();assert.equal(p.requests.at(-1).arguments.refresh,true);p.update(false);await p.reply(p.requests.at(-1),value);await refresh;
 assert.equal(p.node('model-choice').children.length,0);assert.equal(p.node('model-choice-save').disabled,true);
});

test('model and effort changes coalesce into one automatic save; a failure restores the saved pair',async()=>{
 const p=page('model-choice.js');p.body.dataset.navPage='model';p.update(true);await p.reply(p.requests.at(-1),value);
 p.node('model-choice').value='second';p.node('model-choice').onchange();p.node('model-effort').value='ultra';p.node('model-effort').onchange();
 assert.equal(p.requests.filter(r=>r.operation==='model.configure').length,0);
 await p.advance(350);assert.equal(p.requests.filter(r=>r.operation==='model.configure').length,1);
 assert.deepEqual(JSON.parse(JSON.stringify(p.requests.at(-1).arguments)),{account_id:'a',expected:'1',model:'second',effort:'ultra'});
 await p.fail(p.requests.at(-1));assert.equal(p.node('model-choice').value,'first');assert.equal(p.node('model-effort').value,'low');assert.match(p.node('model-choice-status').textContent,/保存失败/);
 assert.equal(p.node('model-catalog-refresh').hidden,false);
 p.node('model-choice').value='second';p.node('model-choice').onchange();p.update(false);await p.advance(350);assert.equal(p.requests.filter(r=>r.operation==='model.configure').length,1);
});
test('a completed authorization renewal reloads a failed catalog without a manual retry',async()=>{
 const p=page('model-choice.js');p.body.dataset.navPage='model';p.update(true);
 await p.reply({request_id:'account-state'},{multi_account:true,active_account:'a',state:'working',busy:true});
 await p.fail(p.requests.at(-1));assert.equal(p.node('model-catalog-refresh').hidden,false);
 const count=p.requests.length;
 await p.reply({request_id:'account-state'},{multi_account:true,active_account:'a',state:'authenticated',busy:false,subscription_permission:true,token_expired:false});
 assert.equal(p.requests.length,count+1);assert.equal(p.requests.at(-1).operation,'model.catalog');
 await p.reply(p.requests.at(-1),value);assert.equal(p.node('model-choice').value,'first');assert.equal(p.node('model-catalog-refresh').hidden,false);assert.equal(p.node('model-catalog-refresh').textContent,'刷新模型目录');
 await p.reply({request_id:'account-state'},{multi_account:true,active_account:'a',state:'authenticated',busy:false,subscription_permission:true,token_expired:false});
 assert.equal(p.requests.length,count+1);
});
