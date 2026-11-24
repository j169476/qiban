// Exercise the desktop controls with synthetic accounts and no external platform connections.
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs/promises');
const {chromium}=require('playwright');
const {bridgeFixture,supported}=require('./check_workspace_browser.cjs');

function accountsFixture(){
 const directories={
  qq:[{id:'10003',label:'摄影群'},{id:'10004',label:'读书群'}],
  wechat:[{id:'999@chatroom',kind:'group',name:'同学群'},{id:'offline@chatroom',kind:'group',name:'仅目录群'}]
 };
 window.fixtureReply=(request,ok,fail)=>{
  const {operation,arguments:a}=request;
  switch(operation){
   case 'qq.status':ok({accounts:[],selected:{uin:'10001',label:'测试账号'},revision:1,
    targets:{groups:['10003'],private_targets:[],authorized:true},directory:{groups:directories.qq}});return true;
   case 'qq.directory':ok({});return true;
   case 'wechat.profiles':ok({accounts:['wxid_synthetic'],selected:'wxid_synthetic',revision:1});return true;
   case 'wechat.directory_sync':ok({items:directories.wechat});return true;
   case 'wechat.targets.saved':ok({version:'v1',targets:[]});return true;
  }
  if(/^(group_modes|reminders)\./.test(operation)){
   window.controlsRequest(request).then(result=>result.ok?ok(result.result):fail(result.error));
   return true;
  }
  return false;
 };
}

function syntheticControls(){
 const groups={'10003':{name:'摄影群',reply_authorized:true},'10004':{name:'读书群',reply_authorized:false},
  '999@chatroom':{name:'同学群',reply_authorized:true}};
 for(const [target,row] of Object.entries(groups))Object.assign(row,{target,listen:false,proactive:false,version:null,excluded:false,
  capabilities:{listen:true,proactive:!target.includes('@'),reminder_delivery:!target.includes('@'),reason:target.includes('@')?'该来源仅支持旁听。':''}});
 groups['offline@chatroom']={target:'offline@chatroom',name:'仅目录群',listen:false,proactive:false,version:null,reply_authorized:false,excluded:false,
  capabilities:{listen:false,proactive:false,reminder_delivery:false,reason:'此群尚无受支持的收件来源。'}};
 const reminders=[{id:'first',version:'initial',label:'提醒 · R0001',recipient_label:'QQ 私聊 · A001',
  due_at:'2031-04-10T01:20:00Z',status:'pending',can_cancel:true,can_reschedule:true,
  clue:'PRIVATE-NOTE-SENTINEL',account:'PRIVATE-ACCOUNT-SENTINEL',target:'PRIVATE-TARGET-SENTINEL'}];
 let revision=0;
 window.controlsRequest=async({operation,arguments:a})=>{
  const ok=result=>({ok:true,result}),fail=error=>({ok:false,error});
  if(window.holdGroup&&operation==='group_modes.list'){
   window.holdGroup=false;
   await new Promise(resolve=>{window.releaseGroup=resolve;});
  }
  switch(operation){
   case 'group_modes.list':{
    let rows=Object.values(groups).filter(r=>r.target.includes('@')===(a.platform==='wechat'));
    if(a.target)rows=rows.filter(r=>r.target===a.target);
    const offset=a.after?rows.findIndex(r=>r.target===a.after)+1:0;
    return ok({items:rows.slice(offset,offset+1),next:rows[offset+1]?rows[offset].target:null});
   }
   case 'group_modes.save':{
    const row=groups[a.target];if(!row||row.version!==a.expected)return fail('edit_conflict');
    if(window.denyGroup){window.denyGroup=false;return fail('群来源已变更，请重新同步。');}
    if(a.proactive&&!row.reply_authorized)return fail('reply_not_authorized');
    Object.assign(row,{listen:a.listen,proactive:a.proactive,version:String(++revision)});return ok(row);
   }
   case 'reminders.list':return ok({items:reminders.filter(r=>(!a.status||r.status===a.status)&&(!a.id||r.id===a.id)),next:null,
    capabilities:{qq:{private:true,group:true},bilibili:{private:true,group:false},wechat:{private:false,group:false}}});
   case 'reminders.reschedule':case 'reminders.cancel':{
    const row=reminders.find(r=>r.id===a.id);
    if(window.forceDelivered){window.forceDelivered=false;Object.assign(row,{status:'sent',version:'changed',can_cancel:false,can_reschedule:false});}
    if(a.expected!==row.version)return fail('edit_conflict');
    if(row.status!=='pending')return fail('reminder_state');
    if(a.due_at)row.due_at=a.due_at;else Object.assign(row,{status:'cancelled',can_cancel:false,can_reschedule:false});
    row.version=String(++revision);return ok(row);
   }
  }
  return fail('unsupported');
 };
}

async function openPage(page,fixture=accountsFixture){
 await page.addInitScript(bridgeFixture,{operations:supported});
 await page.addInitScript(fixture);
 await page.route('https://controls.test/**',route=>{
  const url=new URL(route.request().url());
  if(['/public-content.js','/favicon.ico'].includes(url.pathname))return route.fulfill({status:204});
  const name=url.pathname.startsWith('/room-stills/')?url.pathname.slice(1):path.basename(url.pathname);
  return route.fulfill({path:path.join(__dirname,'Assets',name)});
 });
 await page.goto('https://controls.test/index.html');await page.evaluate(()=>desktopService(true));
}

async function journey(page,{conflict=false,realRace=false,captures}={}){
 const groups=page.locator('#group-modes-panel'),reminders=page.locator('#reminders-panel');
 const selected=groups.locator('details[open]'),edit=reminders.locator('[data-items] details[open]');
 const save=selected.getByRole('button',{name:'保存群设置'}),listen=selected.getByLabel('旁听学习'),active=selected.getByLabel('主动参与');
 const until=async(where,pattern)=>where.locator('[data-notice]').filter({hasText:pattern}).waitFor();
 const group=async(name,target,platform='qq')=>{
  const picker=page.locator(platform==='qq'?'#qq-contact-picker':'#audience-contact-picker');
  await picker.getByRole('button',{name:'设置 '+name+' 的群聊行为'}).click();
  await groups.locator(`details[open][data-target="${target}"]`).waitFor();
 };
 await page.locator('[data-nav-target=platforms]').click();
 await group('读书群','10004');await listen.check();assert.ok(await active.isDisabled());await save.click();await until(groups,'群设置已保存');
 assert.ok(await listen.isChecked());assert.ok(!await active.isChecked());
 await group('摄影群','10003');assert.ok(await active.isDisabled());await listen.check();await active.check();await save.click();
 await until(groups,'群设置已保存');assert.ok(await active.isChecked());
 if(captures){await fs.mkdir(captures,{recursive:true});await groups.screenshot({path:path.join(captures,'qq-group.png')});}
 await listen.uncheck();assert.ok(!await active.isChecked());await save.click();await until(groups,'群设置已保存');
 if(conflict){
  await groups.getByRole('button',{name:'查看全部群'}).click();
  await groups.getByRole('button',{name:'下一页'}).click();await groups.locator('summary').filter({hasText:'读书群'}).waitFor();
  await groups.getByRole('button',{name:'上一页'}).click();await groups.locator('summary').filter({hasText:'摄影群'}).waitFor();
 }
 await page.locator('[data-tab-target=wechat]').click();await group('仅目录群','offline@chatroom','wechat');
 assert.ok(await listen.isDisabled());assert.ok(await active.isDisabled());assert.ok(await save.isDisabled());
 await selected.getByText('此群尚无受支持的收件来源。').waitFor();
 await group('同学群','999@chatroom','wechat');
 assert.ok(!await listen.isChecked());await listen.check();assert.ok(await active.isDisabled());await save.click();await until(groups,'群设置已保存');
 if(captures)await groups.screenshot({path:path.join(captures,'wechat-group.png')});
 await page.locator('[data-nav-target=records]').click();await page.locator('[data-tab-target=reminders]').click();
 const delivery=reminders.locator('[data-delivery]');
 await delivery.locator('summary').click();
 assert.deepEqual((await delivery.locator('li').allTextContents()).sort(),['QQ · 私聊、群聊','B 站 · 私聊','微信 · 暂不支持'].sort());
 await delivery.locator('summary').click();
 const rows=reminders.locator('[data-items] summary');
 const title=await rows.first().locator('strong').innerText();
 const record=rows.filter({hasText:title});await record.click();
 assert.doesNotMatch(await reminders.innerHTML(),/PRIVATE-(?:NOTE|ACCOUNT|TARGET)-SENTINEL/);
 const date=edit.getByLabel('新的提醒时间（本地）');
 const future=(new Date().getUTCFullYear()+1)+'-04-12T16:35';
 await date.fill(future);await edit.getByRole('button',{name:'保存改期'}).click();await until(reminders,'提醒已改期');
 assert.match(await record.innerText(),/16:35/);await record.click();
 assert.equal(await date.inputValue(),future);
 if(captures)await reminders.screenshot({path:path.join(captures,'reminder.png')});
 if(conflict)await page.evaluate(()=>{window.forceDelivered=true;});
 await edit.getByRole('button',{name:'取消提醒'}).click();
 await until(reminders,realRace?'提醒状态已变化：处理中':conflict?'提醒状态已变化：已送达':'提醒已取消');await reminders.locator('[data-filter]').selectOption('all');
 await record.filter({hasText:realRace?'处理中':conflict?'已送达':'已取消'}).waitFor();await record.click();assert.equal(await edit.locator('button').count(),0);
 assert.equal(await page.locator('#tasks-result').isVisible(),false);
 await page.setViewportSize({width:375,height:800});
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 await page.evaluate(()=>access(false));await until(reminders,'解锁桌面管理后可查看提醒');assert.equal(await reminders.locator('[data-items] details').count(),0);
 assert.ok(await reminders.locator('[data-delivery]').isHidden());
}

async function main(){
 const browser=await chromium.launch({headless:true,...(process.env.QIBAN_CHROME?{executablePath:process.env.QIBAN_CHROME}:{})});
 try{
  for(const conflict of [false,true]){
   const p=await browser.newPage({timezoneId:'Asia/Shanghai',viewport:{width:1100,height:900}});
   const errors=[];p.on('pageerror',e=>errors.push(e.message));p.setDefaultTimeout(6500);
   await p.addInitScript(syntheticControls);await openPage(p);
   await journey(p,{conflict,captures:conflict?null:process.env.QIBAN_UI_CAPTURES});
   const saves=await p.evaluate(()=>requests.filter(r=>r.operation==='group_modes.save').map(r=>r.arguments));
   assert.deepEqual(saves.map(s=>[s.platform,s.listen,s.proactive]),[['qq',true,false],['qq',true,true],['qq',false,false],['wechat',true,false]]);
   assert.deepEqual(errors,[]);await p.close();
  }
  const p=await browser.newPage();p.setDefaultTimeout(6500);await p.addInitScript(syntheticControls);
  await openPage(p);await p.locator('[data-nav-target=platforms]').click();
  const groups=p.locator('#group-modes-panel');
  await p.locator('#qq-contact-picker').getByRole('button',{name:'设置 摄影群 的群聊行为'}).click();
  const listen=groups.getByLabel('旁听学习');
  await listen.check();await p.evaluate(()=>{window.denyGroup=true;});
  await groups.getByRole('button',{name:'保存群设置'}).click();await groups.getByText('群来源已变更，请重新同步。').waitFor();
  assert.equal(await listen.isChecked(),false);
  await p.evaluate(()=>{window.holdGroup=true;});
  await groups.getByRole('button',{name:'刷新群设置'}).click();await p.waitForFunction(()=>!!window.releaseGroup);
  await p.locator('[data-tab-target=wechat]').click();await groups.getByText('微信 · wxid_synthetic').waitFor();
  await p.evaluate(()=>window.releaseGroup());await groups.locator('summary').filter({hasText:'同学群'}).waitFor();
  assert.equal(await groups.locator('[data-target="10003"]').count(),0);
  await p.evaluate(()=>{
   const previous=window.controlsRequest;
   window.controlsRequest=async req=>{
    if(req.operation!=='reminders.list')return previous(req);
    const rows=[
     {id:'cancel-only',label:'只能取消',can_cancel:true},
     {id:'reschedule-only',label:'只能改期',can_reschedule:true},
     {id:'legacy',clue:'PRIVATE-NOTE-SENTINEL',target:'PRIVATE-TARGET-SENTINEL'}
    ].map(r=>({...r,status:'pending',version:'v0',due_at:null}));
    return {ok:true,result:{items:rows,next:null}};
   };
  });
  await p.locator('[data-nav-target=records]').click();await p.locator('[data-tab-target=reminders]').click();
  const records=p.locator('#reminders-panel [data-items] details');
  for(const [title,actions] of [['只能取消',['取消提醒']],['只能改期',['保存改期']],['提醒',[]]]){
   const row=records.filter({has:p.getByText(title,{exact:true})});
   await row.locator('summary').click();assert.deepEqual(await row.locator('button').allTextContents(),actions);
   if(title==='只能改期')assert.equal(await row.locator('input').inputValue(),'');
  }
  assert.doesNotMatch(await p.locator('#reminders-panel').innerHTML(),/PRIVATE-(?:NOTE|TARGET)-SENTINEL/);
  await p.close();
  console.log('通过：两平台逐群旁听、主动参与权限及联动、分页、改期、取消、并发到期与锁屏。');
 }finally{await browser.close();}
}
module.exports={openPage,journey};
if(require.main===module)main().catch(e=>{console.error(e);process.exitCode=1;});
