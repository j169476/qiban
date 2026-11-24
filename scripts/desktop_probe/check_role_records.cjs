// Exercise the Windows UI with synthetic records and no external accounts.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
const {openPage}=require('./check_conversation_controls.cjs');

function recordsFixture(){
 window.fixtureReply=(request,ok,fail)=>{
  if(!/^(matters|privacy|sharing|reminders)\./.test(request.operation)&&request.operation!=='runtime.status')return false;
  window.recordsBackend(request).then(reply=>reply.ok?ok(reply.result):fail(reply.error));
  return true;
 };
}

function sampleBackend(race=false){
 const matter={id:'sample',version:'m0',label:'QQ 事项 · M001',action:'chat',status:'waiting',delivery:'sent',
  recipient_label:'QQ 群聊 · G001',wake_at:null,updated_at:'2031-04-10T02:20:00Z',can_stop:true};
 const fragment={id:'f1',version:'f0',kind:'memories',identifier:'sample-note',label:'活动集合地点',status:'reviewed',
  level:'circle',category:'meeting',audience:'本次活动的参与者',choices:['private','circle']};
 const secret={id:'f2',version:'v0',kind:'event',identifier:'event-key',label:'凭据片段',status:'reviewed',
  level:'credential',category:'credential',audience:'仅限受控工具',choices:[],raw:'NEVER-RENDER-THIS-TEST-FIELD'};
 const venue={...fragment,id:'f3',identifier:'venue-note',label:'场馆地点片段',category:'venue',level:'social',
  audience:'适用的 QQ、微信社交会话，B 站公开评论除外',choices:['private','social']};
 const pending={...fragment,id:'f4',identifier:'pending-note',label:'待定片段',status:'pending',category:'unknown',
  level:null,audience:'原资料范围',choices:[]};
 const uncertain={id:'unknown-delivery',version:'u0',label:'QQ 事项 · M002',action:'chat',status:'uncertain',delivery:'uncertain',can_stop:false};
 const timed={id:'alarm',label:'QQ 事项 · M003',action:'alarm',status:'waiting',delivery:'none',wake_at:'2031-04-10T04:00:00Z',can_stop:false};
 const ended={id:'end',label:'QQ 事项 · M004',action:'end',status:'completed',delivery:'none',can_stop:false};
 const wait={id:'wait',label:'QQ 事项 · M005',action:'wait',status:'waiting',delivery:'none',can_stop:false};
 const legacy={id:'old',label:'旧事项 · M006',status:'completed',delivery:'none',can_stop:false,target:'PRIVATE-TARGET-SENTINEL'};
 const rows=[matter,timed,wait,ended,uncertain,legacy];
 return req=>{
  const {operation,arguments:a}=req;const ok=result=>({ok:true,result}),conflict=()=>({ok:false,error:'edit_conflict'});
  switch(operation){
   case 'runtime.status':return ok({platforms:[],activities:[],recent_actions:rows});
   case 'matters.list':return ok({items:rows.filter(row=>!a.id||row.id===a.id),next:null});
   case 'matters.stop':
    if(matter.version!==a.expected)return conflict();
    Object.assign(matter,{status:'stopped',can_stop:false,version:'m1'});return race?conflict():ok(matter);
   case 'privacy.list':{
    const rows=[fragment,secret,venue,pending].filter(r=>(!a.id||r.id===a.id)&&(!a.kind||(r.kind===a.kind&&r.identifier===a.identifier)));
    return ok({items:rows,next:null});
   }
   case 'privacy.correct':
    if(fragment.version!==a.expected)return conflict();
    Object.assign(fragment,{level:a.level,version:'f1',status:'corrected',audience:'已确认的本人私聊'});return race?conflict():ok(fragment);
   case 'sharing.records':return ok({records:[{kind:'memories',identifier:'sample-note',label:'记忆 · sample-note',grants:0}],next:null});
   case 'sharing.record':return ok({kind:a.kind,identifier:a.identifier,policy:{},source:{ok:true},grants:[],controls:{}});
   case 'sharing.targets':return ok({targets:[],next:null});
   default:return {ok:false,error:'unsupported'};
  }
 };
}

async function journey(page,{source='sample-note',captures,race=false}={}){
 const matters=page.locator('#matters-panel'),privacy=page.locator('#privacy-panel');
 const capture=async(node,name)=>{
  if(!captures)return;
  await fs.mkdir(captures,{recursive:true});await node.screenshot({path:path.join(captures,name+'.png')});
 };
 const recent=page.locator('#house-actions');
 const selected=recent.locator('li').filter({hasText:/等待(新消息|唤醒)/}).filter({hasText:'投递已确认'}).first().getByRole('button');
 await selected.click();
 const waiting=matters.locator('.matter-row').filter({hasText:/等待(新消息|唤醒)/}).filter({hasText:'投递已确认'}).first();
 const label=(await waiting.getByRole('button').getAttribute('aria-label')).replace(/^停止续办：/,'');
 assert.equal(await matters.locator('.matter-row').count(),1);
 await capture(matters,'matters');
 await waiting.getByRole('button').click();
 await matters.getByText(race?/事项已变化：已停止/:/已停止后续处理/).waitFor();
 const stopped=matters.locator('.matter-row').filter({hasText:label}).filter({hasText:'已停止 · 投递已确认'}).first();
 await stopped.waitFor();assert.equal(await stopped.getByRole('button').count(),0);
 await matters.getByRole('button',{name:'查看全部事项'}).click();
 const uncertain=matters.locator('.matter-row').filter({hasText:'投递结果不明'});await uncertain.waitFor();
 assert.equal(await uncertain.getByRole('button').count(),0);
 await page.locator('[data-nav-target=persona]').click();
 await page.locator('[data-tab-target=sharing]').click();
 assert.equal(await page.evaluate(()=>requests.filter(r=>r.operation==='privacy.list').length),0);
 await privacy.locator('[data-disclosure]>summary').click();
 const credentials=privacy.locator('[data-items]>details').filter({hasText:'凭据（已遮盖）'}).first();
 await credentials.locator('summary').click();assert.equal(await credentials.locator('select,button').count(),0);
 const pending=privacy.locator('[data-items]>details').filter({hasText:'等待后台审查'}).first();
 await pending.locator('summary').click();assert.equal(await pending.locator('select,button').count(),0);
 const venue=privacy.locator('[data-items]>details').filter({hasText:'普通社交可用'}).first();
 await venue.locator('summary').click();await venue.getByText(/B 站公开评论除外/).waitFor();
 assert.equal(await privacy.innerText().then(t=>t.includes('NEVER-RENDER-THIS-TEST-FIELD')),false);
 await page.locator('#sharing-items').getByRole('button').filter({hasText:source}).first().click();
 await page.getByRole('button',{name:'查看此资料的隐私范围'}).click();
 await privacy.locator('[data-scope]').waitFor();
 const circle=privacy.locator('[data-items]>details').filter({hasText:'圈内共知'}).first();
 await circle.locator('summary').click();
 const select=circle.getByLabel('更正使用范围');
 assert.deepEqual(await select.locator('option').evaluateAll(options=>options.map(o=>o.value)),['','private']);
 assert.ok(await circle.getByRole('button',{name:'保存范围'}).isDisabled());
 await select.selectOption('private');
 await capture(privacy,'privacy-scope');
 await circle.getByRole('button',{name:'保存范围'}).click();
 await privacy.getByText(race?/范围已变化：个人私密/:/使用范围已更新/).waitFor();
 const corrected=privacy.locator('[data-items]>details').filter({hasText:'个人私密'}).filter({hasText:'已纠正'}).first();
 await corrected.locator('summary').click();
 await corrected.getByText(/当前受众：.+/).waitFor();
 await privacy.getByRole('button',{name:'查看全部资料'}).click();
 await credentials.waitFor();
 await page.setViewportSize({width:375,height:800});
 const overflow=await page.evaluate(()=>Array.from(document.querySelectorAll('body *')).filter(n=>n.getBoundingClientRect().right>innerWidth+1)
  .slice(0,15).map(n=>n.tagName+'#'+n.id+'.'+n.className));
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),overflow.join(', '));
 await page.evaluate(()=>access(false));
 assert.equal(await privacy.locator('[data-items]>details').count(),0);
 assert.equal(await matters.locator('.matter-row').count(),0);
}

async function backgroundReview(browser){
 const page=await browser.newPage({viewport:{width:1100,height:900}});
 const backend=sampleBackend();
 let pending=true,hold=false,release,started;
 const reading=new Promise(resolve=>{started=resolve;});
 await page.exposeFunction('recordsBackend',async req=>{
  if(req.operation==='privacy.list'&&hold){
   started();await new Promise(resolve=>{release=resolve;});
  }
  const result=backend(req);
  if(req.operation==='privacy.list'&&pending){
   const [row]=result.result.items;
   return {ok:true,result:{items:[{...row,level:null,status:'pending',choices:[]}],next:null}};
  }
  return result;
 });
 await openPage(page,recordsFixture);
 await page.locator('[data-nav-target=persona]').click();await page.locator('[data-tab-target=sharing]').click();
 const panel=page.locator('#privacy-panel');
 await panel.locator('[data-disclosure]>summary').click();
 const details=panel.locator('[data-items]>details').first();
 await details.locator('summary').filter({hasText:'等待后台审查'}).click();pending=false;
 await details.locator('summary').filter({hasText:'已审查'}).waitFor({timeout:23000});
 assert.equal(await details.evaluate(n=>n.open),true);
 hold=true;await panel.getByRole('button',{name:'刷新范围'}).click();await reading;
 await page.evaluate(()=>access(false));release();
 await page.waitForFunction(()=>!document.querySelector('#privacy-panel [data-items]').childElementCount);
 assert.ok(await panel.locator('[data-disclosure]').evaluate(n=>!n.open));
 await page.close();
}

async function monitorChanges(browser){
 const page=await browser.newPage({timezoneId:'Asia/Shanghai',viewport:{width:1100,height:900}});
 const backend=sampleBackend();
 let recent=[],delayNext=false,release,markHeld;
 const held=new Promise(resolve=>{markHeld=resolve;});
 await page.exposeFunction('recordsBackend',async req=>{
  if(req.operation!=='runtime.status')return backend(req);
  if(delayNext){delayNext=false;await new Promise(resolve=>{release=resolve;markHeld();});}
  return {ok:true,result:{platforms:[],activities:[],recent_actions:recent}};
 });
 await openPage(page,recordsFixture);
 const card=page.locator('#house-actions');
 await card.getByText('暂无行动记录。').waitFor();
 recent=[{id:'one',label:'事项 · A1',status:'waiting',action:'wait',wake_at:'2031-04-10T04:00:00Z',delivery:'none'}];
 await page.locator('#runtime-refresh').click();
 await card.getByText(/等待唤醒.*12:00/).waitFor();
 await page.locator('#runtime-observe').click();
 recent=[{...recent[0],status:'completed',action:'end',wake_at:null}];
 await page.getByText('观察已暂停',{exact:true}).waitFor();
 assert.match(await card.innerText(),/等待唤醒/);
 await page.locator('#runtime-observe').click();
 await card.getByText('已结束',{exact:true}).waitFor();
 delayNext=true;
 await page.locator('#runtime-refresh').click();
 await held;
 await page.evaluate(()=>desktopService(false));
 await card.waitFor({state:'hidden'});assert.equal(await card.locator('li').count(),0);
 // The reply from the disconnected service arrives after the clear.
 release();
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 assert.equal(await card.locator('li').count(),0);
 await page.close();
}

async function main(){
 const browser=await chromium.launch({headless:true,...(process.env.QIBAN_CHROME?{executablePath:process.env.QIBAN_CHROME}:{})});
 try{
  for(const race of [false,true]){
   const page=await browser.newPage({viewport:{width:1100,height:900}}),errors=[];
   page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(6500);
   await page.exposeFunction('recordsBackend',sampleBackend(race));
   await openPage(page,recordsFixture);
   const recent=page.locator('#house-actions'),items=recent.locator('li');
   await items.filter({hasText:'等待新消息'}).first().waitFor();
   assert.deepEqual(await items.locator('strong').allTextContents(),
    ['QQ 事项 · M001 · 会话消息','QQ 事项 · M003 · 定时提醒','QQ 事项 · M005 · 等待','QQ 事项 · M004 · 结束','QQ 事项 · M002 · 会话消息','旧事项 · M006']);
   await items.first().getByText('收件会话：QQ 群聊 · G001').waitFor();
   assert.match(await items.nth(1).innerText(),/等待唤醒.*2031/);
   assert.equal(await items.last().locator('time').count(),0);
   assert.doesNotMatch(await recent.innerHTML(),/PRIVATE-TARGET-SENTINEL/);
   await journey(page,{race,captures:race?null:process.env.QIBAN_UI_CAPTURES});
   assert.equal(await recent.locator('li').count(),0);
   const changes=await page.evaluate(()=>requests.filter(r=>['privacy.correct','matters.stop'].includes(r.operation)).map(r=>r.arguments));
   assert.deepEqual(changes,[{id:'sample',expected:'m0'},{id:'f1',expected:'f0',level:'private'}]);
   assert.deepEqual(errors,[]);await page.close();
  }
  await monitorChanges(browser);
  await backgroundReview(browser);
  console.log('通过：自主事项及投递状态、停止、按来源查看隐私、限定更正选项、凭据遮盖、窄屏与锁屏。');
 }finally{await browser.close();}
}
module.exports={recordsFixture,journey};
if(require.main===module)main().catch(e=>{console.error(e);process.exitCode=1;});
