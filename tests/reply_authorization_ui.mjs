import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const script = readFileSync(new URL('../src/companion_service/static/management.js', import.meta.url), 'utf8');
class Element {
  children = []; listeners = {}; textContent = ''; disabled = false;
  set innerHTML(_) { throw new Error('Untrusted labels must use textContent'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
}
function page({kind='admin', authenticated=true, listing, policies, fail=false, confirmed=true}={}) {
  const nodes = new Map(), handlers = new Map(), calls = [], notices = [];
  const context = {kind, confirm:()=>confirmed, location: {hash:''}, document: {
    body: {dataset: {authenticated: String(authenticated)}}, createElement: () => new Element(),
  }, $: selector => {if(!nodes.has(selector))nodes.set(selector,new Element()); return nodes.get(selector);},
    bind: (selector, callback) => handlers.set(selector, callback), notice: text => notices.push(text),
    operate: async (operation, args) => {
      calls.push({operation, args});
      if(operation==='reply_authorization_list')return typeof listing==='function'?listing(args):listing;
      if(operation==='reply_policy_list')return policies;
      if(fail)throw new Error('edit_conflict');
      return {platform_paused:true};
    }};
  vm.runInNewContext(script, context);
  context.$('#reply-search-platform').value='qq';context.$('#reply-search-query').value='';
  return {nodes, handlers, calls, notices};
}
const item = {id:'stable-scope', version:'current-revision', enabled:false, verified_target:true,
  label:'<img src=x onerror=bad()>', account_label:'synthetic', restriction:'owner only',
  scope:{platform:'wechat', account_identity:'wechat-db:synthetic', kind:'group', target_id:'synthetic@chatroom'}};
const listing = {items:[item], platforms:{wechat:{available:true},qq:{available:false}}};

test('management card renders opaque IDs safely and submits exact scope/CAS', async () => {
  const p=page({listing});
  await p.handlers.get('#load-reply-authorizations')();
  assert.match(p.nodes.get('#reply-platforms').textContent, /QQ.*未接入/);
  const card=p.nodes.get('#reply-authorizations').children[0];
  assert.match(card.children[0].textContent, /<img/);
  assert.match(card.children[1].textContent, /synthetic@chatroom/);
  await card.children.at(-1).listeners.click();
  assert.equal(JSON.stringify(p.calls[1]),JSON.stringify({operation:'reply_authorization_set',args:{
    scope_id:'stable-scope',enabled:true,expected_version:'current-revision'}}));
  assert.match(p.notices[0], /暂停.*重新启动/);
  assert.equal(p.calls[2].operation,'reply_authorization_list');
});

test('stale writes are not retried, revoke uses false, vanished targets cannot be regranted', async () => {
  const p=page({listing:{...listing,items:[{...item,enabled:true,verified_target:false}]},fail:true});
  await p.handlers.get('#load-reply-authorizations')();
  const button=p.nodes.get('#reply-authorizations').children[0].children.at(-1);
  assert.match(button.textContent,/撤销/);
  await button.listeners.click();
  assert.equal(p.calls[1].args.enabled,false);
  assert.equal(p.calls.length,2);
  assert.match(p.notices[0],/edit_conflict.*重新读取/);
  const gone=page({listing:{...listing,items:[{...item,verified_target:false}]}});
  await gone.handlers.get('#load-reply-authorizations')();
  assert.equal(gone.nodes.get('#reply-authorizations').children[0].children.length,3);
});

test('role and unauthenticated pages do not bind management actions', () => {
  assert.equal(page({kind:'role'}).handlers.size,0);
  assert.equal(page({authenticated:false}).handlers.size,0);
});

test('search requires full ID and selected platform; pagination sends a bounded request', async()=>{
  const p=page({listing:{...listing,total:42,offset:0,next_offset:20}});
  await p.handlers.get('#reply-search-form')();assert.equal(p.calls.length,0);
  p.nodes.get('#reply-search-query').value=' 10002 ';
  await p.handlers.get('#reply-search-form')();
  assert.deepEqual(JSON.parse(JSON.stringify(p.calls[0].args)),{platform:'qq',query:'10002',view:'search',offset:0,limit:20});
  assert.equal(p.nodes.get('#reply-next').disabled,false);
  await p.handlers.get('#reply-next')();assert.equal(p.calls[1].args.offset,20);
  p.handlers.get('#reply-search-platform')();
  assert.equal(p.nodes.get('#reply-authorizations').children.length,0);
  assert.equal(p.nodes.get('#reply-pagination').hidden,true);
});

test('cancelling explicit confirmation never changes authorization', async()=>{
  const p=page({listing,confirmed:false});await p.handlers.get('#load-reply-authorizations')();
  await p.nodes.get('#reply-authorizations').children[0].children.at(-1).listeners.click();
  assert.equal(p.calls.length,1);
});

test('changing platform invalidates an in-flight search', async()=>{
  let finish;const p=page({listing:()=>new Promise(resolve=>{finish=resolve;})});
  const pending=p.handlers.get('#load-reply-authorizations')();
  p.handlers.get('#reply-search-platform')();finish(listing);await pending;
  assert.equal(p.nodes.get('#reply-authorizations').children.length,0);
});

test('participation editor uses stable account and CAS and displays distinct private counters', async () => {
  const settings={private_daily_replies:200,group_session_replies:5,group_session_decisions:20,group_session_minutes:10};
  const policies={items:[{platform:'qq',account_identity:'10001',account_label:'synthetic',version:'p1',settings,
    private_usage:{confirmed:2,reserved:1,uncertain:1}}],bounds:Object.fromEntries(Object.keys(settings).map(k=>[k,[0,10000]]))};
  const p=page({policies});await p.handlers.get('#load-reply-policies')();
  const form=p.nodes.get('#reply-policies').children[0];
  assert.match(form.children[1].textContent,/成功 2.*处理中 1.*不确定 1/);
  form.children[2].children[0].children[0].value='12';
  await form.listeners.submit({preventDefault(){}});
  assert.equal(JSON.stringify(p.calls[1]),JSON.stringify({operation:'reply_policy_set',args:{
    platform:'qq',account_identity:'10001',expected_version:'p1',changes:{...settings,private_daily_replies:12}}}));
  assert.equal(p.calls[2].operation,'reply_policy_list');
  assert.match(p.notices[0],/未启动角色/);
});
