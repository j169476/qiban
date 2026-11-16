const test=require('node:test'),assert=require('node:assert/strict');
const page=require('./contacts_test_harness.cjs');
const value={account_id:'a',revision:'1',model:'first',effort:'low',available:true,models:[{id:'first',name:'First',efforts:['low','high'],default_effort:'low'},{id:'second',name:'Second',efforts:['medium','ultra'],default_effort:'medium'}]};
test('model and reasoning choices follow upstream; explicit save sends the selected pair',async()=>{
 const p=page('model-choice.js');p.body.dataset.navPage='model';p.update(true);assert.equal(p.requests.at(-1).operation,'model.catalog');await p.reply(p.requests.at(-1),value);
 assert.equal(p.node('model-effort').value,'low');p.node('model-choice').value='second';p.node('model-choice').onchange();
 assert.deepEqual(p.node('model-effort').children.map(o=>o.value),['medium','ultra']);assert.equal(p.node('model-effort').value,'medium');
 p.node('model-effort').value='ultra';const save=p.node('model-choice-save').onclick();assert.deepEqual(JSON.parse(JSON.stringify(p.requests.at(-1).arguments)),{account_id:'a',expected:'1',model:'second',effort:'ultra'});
 await p.reply(p.requests.at(-1),{...value,model:'second',effort:'ultra',revision:'2'});await save;assert.match(p.node('model-choice-status').textContent,/已保存/);
 const refresh=p.node('model-catalog-refresh').onclick();assert.equal(p.requests.at(-1).arguments.refresh,true);p.update(false);await p.reply(p.requests.at(-1),value);await refresh;
 assert.equal(p.node('model-choice').children.length,0);assert.equal(p.node('model-choice-save').disabled,true);
});
