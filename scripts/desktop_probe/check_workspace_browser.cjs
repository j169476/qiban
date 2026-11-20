// Run with Playwright available to Node. All management traffic stays inside the fixture.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs/promises');
const {execFileSync}=require('node:child_process');
const os=require('node:os');
const assets=path.join(__dirname,'Assets');
const supported=['learning.goals','learning.snapshot','learning.tile','learning.search','learning.detail','learning.edit','learning.index_control','context.workspace'];

function bridgeFixture({operations}){
 const listeners=[], requests=window.requests=[];
 let snap=1,notes={};
 const initial={guidance:{},limits:{},order:['memory_records','summary','relations','growth','catalog','files','stickers']};
 let persona={version:'p1',data:{name:'窗边来客',core:{description:'一份隔离的人物资料',speaking_style:[]},interests:[]}};
 let chat=[{id:'before',speaker:'user',text:'会客室历史来信'}],running=true;
 let chosen='a',effort='low',modelRevision='m1',ultrafast=false;
 const catalog=()=>({account_id:'desktop-fixture',model:chosen,effort,ultrafast,revision:modelRevision,available:true,
  models:[{id:'a',name:'测试模型 A',efforts:['low','new-level'],default_effort:'low',ultrafast:true},{id:'auto',name:'测试模型 B',efforts:[],default_effort:null,ultrafast:false}]});
 const nodes=()=>[{id:'math',title:'验收项',type:'criterion',version:'v1',body:'',x:0,y:0,parent_id:null,prerequisites:[],archived:false},...Object.values(notes)];
 const workspace=()=>({active:{version:'initial',config:initial},scenes:['private','group'],layers:initial.order});
 const report={after:{chars:25,estimated_tokens:60},before:{chars:40},limits:{reply_output_reserve:10},sections:{summary:{kept:1,offered:2,duplicates:1}}};
 window.allowed=operations;window.pendingReads=1;
 function respond(req){
  const a=req.arguments,op=req.operation;
  const emit=(response)=>listeners.forEach(fn=>fn({data:{type:'management',request_id:req.request_id,response:structuredClone(response)}}));
  const ok=result=>emit({ok:true,result}),fail=error=>emit({ok:false,error,message:error});
  if(window.fixtureReply?.(req,ok,fail))return;
  switch(op){
   case 'workspace.capabilities':return ok({operations:window.allowed,context_scope:'installation',context_routes:['local','qq'],trace_detail:'reply_trace.detail'});
   case 'session.status':return ok({locked:!!window.fixtureLocked,room_session:window.fixtureLocked?null:'unit'});
   case 'config.read':return a.kind==='persona'?ok(persona):fail('unsupported');
   case 'config.patch':
    if(window.rejectEdit){window.rejectEdit=false;return fail('修改未保存');}
    if(a.kind!=='persona'||a.expected!==persona.version)return fail('edit_conflict');
    persona={version:'p2',data:{...persona.data,...a.changes}};return ok({version:persona.version});
   case 'backups.list':return ok({backups:[{name:'验收备份',bytes:1280}]});
   case 'notifications.page':return ok({items:[],unread:0,total:0,page:1,pages:1});
   case 'runtime.status':return ok({platforms:['local','bilibili','qq','wechat'].map((platform,i)=>({platform,status:i===1&&running?'active':'stopped',available:true,model_ready:true,read_only:i===1})),
    active_preset:window.fixtureActive||{id:'default',name:'默认',revision:'initial'},runtime_instance:'fixture',activities:running?[{platform:'bilibili',state:window.activity||'watching'}]:[],preference_revision:'pref1',generated_at:'2026-11-19T08:00:00Z'});
   case 'runtime.control':running=a.action!=='stop';return ok({});
   case 'room.history':return ok({messages:chat,next:null});
   case 'room.post':chat.push({id:'new',text:a.text,speaker:'user'});return ok({accepted:true});
   case 'room.search':return ok({messages:chat.filter(m=>m.text.includes(a.query)),next:null});
   case 'room.queue':return ok({messages:[]});
   case 'tasks.list':return ok({items:[],capabilities:{video:{available:true},web:{available:true},learn:{available:true}}});
   case 'model.catalog':return ok(catalog());
   case 'model.configure':
    if(a.account_id!=='desktop-fixture'||a.expected!==modelRevision)return fail('edit_conflict');
    chosen=a.model;effort=a.effort;ultrafast=a.ultrafast;modelRevision='m2';return ok(catalog());
   case 'learning.goals':return ok({account:'test-account',items:[{id:'first',definition:'D1',title:'第一棵树'},{id:'second',definition:'D2',title:'第二棵树'}],after:null});
   case 'learning.snapshot':{
    if(window.indexPaused)return ok({ready:false,snapshot:null,index:{paused:true,pending:1,last_success:null,error:null}});
    if(window.pendingReads-->0)return ok({ready:false,index:{paused:false,pending:1}});
    return ok({ready:true,snapshot:String(snap),bounds:[0,0,2048,8192],tile_size:1024,count:nodes().length,index:{}});
   }
   case 'learning.index_control':window.indexPaused=a.paused;return ok({paused:a.paused});
   case 'learning.tile':case 'learning.search':{
    const list=nodes().filter(n=>op==='learning.tile'?(n.x>>10+a.level)===a.x&&(n.y>>10+a.level)===a.y:n.title.includes(a.query));
    return ok({items:list,total:list.length,snapshot:String(snap),next_offset:null});
   }
   case 'learning.detail':{
    const item=nodes().find(n=>n.id===a.node);
    if(window.holdDetail){window.releaseDetail=()=>ok(item);return;}
    return item?ok(item):fail('learning_unavailable');
   }
   case 'learning.edit':{
    if(a.account!=='test-account'||a.definition!==(a.goal==='first'?'D1':'D2')||a.snapshot!==String(snap))return fail('management_request');
    const known=nodes().find(n=>n.id===a.node);
    if(window.conflict){window.conflict=false;snap++;return fail('edit_conflict');}
    if((known?.version??null)!==a.expected)return fail('edit_conflict');
    notes[a.node]={...(known||{id:a.node,x:256,y:96,type:'note'}),...a.changes,version:'v'+(++snap)};
    return ok({node:a.node,version:notes[a.node].version,refresh:'learning.snapshot'});
   }
   case 'context.workspace':return ok(workspace());
   case 'reply_trace.list':return ok({items:[{id:'trace',platform:'wechat',started:'2026-01-01T00:00:00Z',duration_ms:7109,status:'completed',outcome:'cancelled'}]});
   case 'reply_trace.detail':return ok({id:'trace',platform:'wechat',started:'2026-01-01T00:00:00Z',duration_ms:7109,status:'completed',outcome:'cancelled',outcome_error:'wechat_driver_prepare_failed',steps:[{stage:'context_budget',workspace_version:'pinned',...report}],sources:[{resource:'visible/source',version:'v1',version_current:false}]});
   default:return fail('unsupported');
  }
 }
 window.uninstallState=state=>listeners.forEach(fn=>fn({data:{type:'uninstall',state}}));
 (window.chrome??={}).webview={addEventListener:(type,fn)=>{if(type==='message')listeners.push(fn);},postMessage(req){
  if(req.action.startsWith('uninstall-')){requests.push(structuredClone(req));return;}
  if(req.action!=='management'&&!['library-replace','library-export-version'].includes(req.action))return;
  requests.push(structuredClone(req));setTimeout(()=>respond(req),req.operation==='learning.snapshot'?85:12);
 }};
 window.access=(ready)=>dispatchEvent(new CustomEvent('qiban-management-state',{detail:{ready,locked:!ready,roomSession:ready?'unit':null}}));
 window.desktopService=ready=>listeners.forEach(fn=>fn({data:{type:'service',ready,management:true,state:ready?'ready':'stopped',members:[],windowsUser:'测试用户'}}));
}

async function checkHouse(p){
 const nav=name=>p.locator('[data-nav-target="'+name+'"]').click();
 const call=op=>p.waitForFunction(op=>requests.some(r=>r.operation===op),op);
 const checkWidth=async()=>{
  const outside=await p.evaluate(()=>[...document.querySelectorAll('body *')].filter(n=>n.getBoundingClientRect().right>innerWidth+1)
   .map(n=>n.tagName+'#'+n.id+'.'+String(n.className)).slice(0,12));
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,outside.join(', '));
 };
 const waitActor=pose=>p.waitForFunction(pose=>{
  const w=document.getElementById('monitor-window');
  const correctPose=pose==='idle'?['reading','sleeping','window','tail'].includes(w.dataset.pose):w.dataset.pose===pose;
  return correctPose && w.dataset.phase==='still' && w.actor?.querySelector('img')?.naturalWidth>0;
 },pose);
 const draw=async name=>{
  if(!process.env.QIBAN_UI_CAPTURES)return;
  await fs.mkdir(process.env.QIBAN_UI_CAPTURES,{recursive:true});
  await p.screenshot({path:path.join(process.env.QIBAN_UI_CAPTURES,name+'.png')});
 };
 await p.evaluate(()=>desktopService(true));
 await p.locator('#monitor-state').filter({hasText:'正在观看视频'}).waitFor();
 await waitActor('watching');
 assert.ok(await p.locator('#monitor-window').evaluate(w=>w.dataset.rendering==='pixel'&&w.base.naturalWidth===w.media.size[0]&&getComputedStyle(w.actor.firstChild).imageRendering==='pixelated'));
 assert.equal(await p.locator('iframe').count(),0);assert.equal(await p.locator('#runtime-platforms details').count(),4);
 assert.equal(await p.locator('[data-nav-target=home]').getAttribute('aria-current'),'page');
 await draw('01-monitor');
 const actorGeneration=await p.locator('#monitor-window').evaluate(w=>w.generation);
 await p.locator('#runtime-refresh').click();await p.waitForFunction(()=>!document.querySelector('#runtime-refresh').disabled);
 assert.equal(await p.locator('#monitor-window').evaluate(w=>w.generation),actorGeneration);
 const background=await p.locator('#monitor-window .room-base').getAttribute('src');
 for(const [action,label] of [['replying','正在撰写回复'],['learning','正在学习'],['watching','正在观看视频']]){
  await p.evaluate(k=>{window.activity=k;},action);await p.locator('#runtime-refresh').click();
  await p.locator('#monitor-state').filter({hasText:label}).waitFor();await waitActor(action);
  assert.equal(await p.locator('#monitor-window .room-base').getAttribute('src'),background);
 }
 await p.emulateMedia({reducedMotion:'reduce'});await waitActor('watching');
 await p.emulateMedia({reducedMotion:'no-preference'});await waitActor('watching');
 await p.locator('#runtime-observe').click();await p.getByText('观察已暂停',{exact:true}).waitFor();
 await waitActor('watching');
 await p.locator('#runtime-observe').click();await p.locator('#monitor-state').filter({hasText:'正在观看视频'}).waitFor();
 await p.locator('[data-platform=bilibili] summary').click();await p.locator('[data-platform=bilibili] [data-control=stop]').click();
 await p.waitForFunction(()=>requests.some(r=>r.operation==='runtime.control'&&r.arguments.platform==='bilibili'&&r.arguments.action==='stop'));
 await p.locator('#monitor-state').filter({hasText:'休息中'}).waitFor();
 await nav('persona');await p.locator('#persona-name').fill('仅用于前端验收');await p.evaluate(()=>{window.rejectEdit=true;});
 await p.locator('#save-persona').click();await p.locator('#profile-notice').filter({hasText:'修改未保存'}).waitFor();
 await p.locator('#save-persona').click();await p.locator('#profile-notice').filter({hasText:'资料已保存'}).waitFor();
 await p.locator('#room-open').click();await p.locator('#room-history').filter({hasText:'会客室历史来信'}).waitFor();
 await p.locator('#room-text').fill('留在会客室的草稿');
 await p.locator('.house-public-links a[href="#declaration"]').click();await p.locator('#reader-body h2').first().waitFor();
 await p.keyboard.press('Escape');await p.locator('#public-reader').waitFor({state:'hidden'});
 assert.equal(await p.locator('#room-text').inputValue(),'留在会客室的草稿');assert.ok(await p.locator('#room-dialog').isVisible());
 await p.locator('#room-text').fill('本地验收消息');await p.locator('#room-send').click();await p.locator('#room-history').filter({hasText:'本地验收消息'}).waitFor();
 await p.locator('#room-query').fill('历史来信');await p.locator('#room-search').click();await call('room.search');
 await p.locator('#room-close').click();
 await nav('model');await p.locator('#model-effort-choices button').filter({hasText:'new-level'}).click();
 await p.locator('#model-applied').filter({hasText:'new-level'}).waitFor();await draw('02-model');
 const speed=p.locator('#model-ultrafast');assert.equal(await speed.getAttribute('aria-pressed'),'false');await speed.click();
 await p.waitForFunction(()=>requests.some(r=>r.operation==='model.configure'&&r.arguments.ultrafast===true));
 await p.locator('#model-choice').selectOption('auto');await p.locator('#model-applied').filter({hasText:'由模型决定'}).waitFor();assert.ok(await speed.isHidden());
 const modelWrites=await p.evaluate(()=>requests.filter(r=>r.operation==='model.configure').map(r=>r.arguments));
 assert.deepEqual(modelWrites.at(-1),{account_id:'desktop-fixture',expected:'m2',model:'auto',effort:null,ultrafast:false});
 await nav('platforms');await p.locator('[data-tab-target=wechat]').click();assert.ok(await p.locator('#audience-panel').isVisible());
 await nav('records');await call('tasks.list');await p.locator('[data-tab-target=trace]').click();await call('reply_trace.list');
 await p.locator('#reply-trace-list button').first().click();await call('reply_trace.detail');
 await p.locator('#reply-trace-detail').getByText('记录状态：未发送').waitFor();await p.locator('#reply-trace-detail').getByText(/微信发送准备失败/).waitFor();
 await nav('media');await p.locator('[data-tab-target=library]').click();await p.locator('[data-tab-target=stickers]').click();
 await nav('settings');assert.equal(await p.locator('#backup-selection option').first().textContent(),'验收备份 · 1.3 KB');
 assert.equal(await p.evaluate(()=>requests.some(r=>r.operation==='backups.restore')),false);
 await nav('home');await p.setViewportSize({width:390,height:844});
 await p.locator('#house-menu').click();await p.locator('[data-nav-target=persona]').click();
 assert.ok(await p.locator('#persona-name').isVisible());
 await checkWidth();
 await p.locator('#house-monitor').click();await waitActor('idle');await draw('03-narrow');
 await checkWidth();await p.setViewportSize({width:1250,height:870});
 await p.evaluate(()=>desktopService(false));await p.waitForFunction(()=>document.querySelector('#runtime-platforms').childElementCount===0);
 assert.equal(await p.locator('#house-events').textContent(),'');assert.equal(await p.locator('#persona-name').inputValue(),'');
}

async function checkPublicReader(p){
 const dialog=p.locator('#public-reader'),article=p.locator('#reader-body');
 const externalRequests=[];
 const inspect=request=>{if(new URL(request.url()).hostname!=='workspace.test')externalRequests.push(request.url());};
 p.on('request',inspect);await p.context().setOffline(true);
 const enter=async name=>{const link=p.locator('.house-public-links a[href="#'+name+'"]');await link.focus();await p.keyboard.press('Enter');await dialog.waitFor();};
 const close=async key=>{if(key)await p.keyboard.press(key);else await p.locator('#reader-back').click();await dialog.waitFor({state:'hidden'});};
 const rows=async (name)=>{
  const raw=await fs.readFile(path.join(__dirname,'../../docs',name),'utf8');
  return raw.split(/\r?\n/).filter(line=>line.startsWith('|')&&!/^\|[\s|:-]+$/.test(line))
   .map(line=>line.slice(1,-1).split('|').map(s=>s.trim()));
 };
 const displayedRows=()=>article.locator('tr').evaluateAll(rs=>rs.map(r=>Array.from(r.cells,c=>c.textContent)));
 await p.goto('https://workspace.test/index.html');
 const before=await p.evaluate(()=>requests.length);
 await enter('declaration');await article.getByText('开发者：Yi_Ning和ta的龙娘').waitFor();
 await article.locator('summary').filter({hasText:'MIT'}).click();await article.locator('pre').first().filter({hasText:'Permission is hereby granted'}).waitFor();
 const upstream=article.locator('.reader-upstreams');await upstream.locator('summary').click();
 const source=await fs.readFile(path.join(__dirname,'../../THIRD_PARTY_NOTICES.md'),'utf8');
 const index=source.split('## 桌面许可索引')[1].split(/^## /m)[0];
 const links=[...index.matchAll(/\[([^\]]+)\]\((https:\/\/[^)]+)\)/g)].map(m=>[m[1],m[2]]);
 assert.ok(links.length>0);
 assert.deepEqual(await upstream.locator('a').evaluateAll(nodes=>nodes.map(n=>[n.textContent,n.href])),links);
 const firstProject=links[0][1];
 await p.context().route(firstProject,route=>route.fulfill({contentType:'text/html',body:'<title>上游仓库</title>'}));
 const [outside]=await Promise.all([p.waitForEvent('popup'),upstream.locator('a').first().click()]);
 await outside.waitForURL(firstProject);await outside.close();await p.context().unroute(firstProject);
 assert.ok(await dialog.isVisible());
 assert.equal(await upstream.locator('pre').count(),0);assert.equal(await upstream.locator('tbody tr').count(),index.split(/\r?\n/).filter(l=>l.startsWith('|')).length-2);
 await p.setViewportSize({width:360,height:650});
 assert.ok(await upstream.evaluate(n=>n.scrollWidth<=n.clientWidth+1));
 await p.setViewportSize({width:1250,height:870});
 await close('Escape');
 assert.equal(await p.evaluate(()=>document.activeElement.hash),'#declaration');
 await enter('support');await article.getByRole('heading',{name:'可用版本'}).waitFor();
 const wechatSource=await fs.readFile(path.join(__dirname,'../../docs/微信支持范围.md'),'utf8');
 const versionNumbers=text=>text.match(/\b\d+\.\d+\.\d+\.\d+\b/g)||[];
 const availableVersions=versionNumbers(wechatSource);
 assert.ok(availableVersions.length);
 assert.deepEqual(await article.locator('li').allTextContents(),availableVersions);
 const expectedBody=wechatSource.replace(/^(?:#+\s+|-\s+)/gm,'').trim().split(/\s+/);
 assert.deepEqual((await article.innerText()).trim().split(/\s+/),expectedBody);
 await p.locator('[data-reader-part=costs]').click();await article.locator('table').first().waitFor();
 assert.equal(await article.locator('table').count(),1);assert.deepEqual(await displayedRows(),await rows('模型选择参考.md'));
 await p.goBack();await dialog.waitFor({state:'hidden'});assert.equal(await p.evaluate(()=>requests.length),before);
 await p.goForward();await article.getByRole('heading',{name:'可用版本'}).waitFor();await close();

 await p.setViewportSize({width:360,height:650});await p.evaluate(()=>{window.fixtureLocked=true;desktopService(true);});
 await p.locator('#management-panel[data-locked=true]').waitFor();
 await p.locator('#house-menu').click();await enter('support');await article.getByRole('heading',{name:'可用版本'}).waitFor();
 assert.equal(await p.locator('#unlock-password').inputValue(),'');
 await p.locator('[data-reader-part=costs]').click();await article.locator('table').waitFor();
 assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth&&document.querySelector('#reader-body').scrollWidth<=document.querySelector('#reader-body').clientWidth+1),true);
 const wide=article.locator('.reader-table').last();await wide.focus();await p.keyboard.press('End');
 assert.equal(await wide.evaluate(t=>t.scrollWidth>t.clientWidth),true);
 if(process.env.QIBAN_UI_CAPTURES){await fs.mkdir(process.env.QIBAN_UI_CAPTURES,{recursive:true});await p.screenshot({path:path.join(process.env.QIBAN_UI_CAPTURES,'04-public-narrow.png')});}
 await close('Escape');assert.equal(await p.evaluate(()=>document.activeElement.hash),'#support');
 await p.setViewportSize({width:1250,height:870});await p.goto('https://workspace.test/index.html#support');await article.getByRole('heading',{name:'可用版本'}).waitFor();
 const contrast=await p.locator('.reader-choices button').evaluateAll(buttons=>buttons.map(button=>{
  const style=getComputedStyle(button);
  const lum=rgb=>rgb.match(/\d+/g).slice(0,3).map(Number).reduce((sum,channel,i)=>{
   const c=channel/255;return sum+[.2126,.7152,.0722][i]*(c<=.04045?c/12.92:((c+.055)/1.055)**2.4);
  },0);
  const a=lum(style.color),b=lum(style.backgroundColor);
  return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
 }));
 assert.ok(contrast.every(n=>n>=4.5),'支持页签的文字对比度 '+contrast.join(' / '));
 if(process.env.QIBAN_UI_CAPTURES){
  await p.screenshot({path:path.join(process.env.QIBAN_UI_CAPTURES,'05-public-support.png')});
  await p.locator('[data-reader-part=costs]').click();await article.locator('table').waitFor();
  await p.screenshot({path:path.join(process.env.QIBAN_UI_CAPTURES,'06-model-reference.png')});
 }
 await close();assert.equal(await p.evaluate(()=>location.hash),'');
 assert.deepEqual(externalRequests,[]);p.off('request',inspect);await p.context().setOffline(false);
}

async function checkUninstall(p){
 const option=p.locator('.uninstall-clear-option'),box=option.getByRole('checkbox'),skip=p.locator('#uninstall-skip'),next=p.locator('#uninstall-continue');
 const preparing={registered:true,backup_available:true,backup:'none',can_clear_profile:true,continue_refusal:'uninstall_backup_incomplete'};
 const state=overrides=>p.evaluate(s=>uninstallState(s),{...preparing,...overrides});
 const colors=button=>button.evaluate(async b=>{
  await Promise.all(b.getAnimations().map(animation=>animation.finished));
  const css=getComputedStyle(b);return [css.backgroundColor,css.color,css.borderColor];
 });
 for(const width of [1250,360]){
  await p.setViewportSize({width,height:800});
  await p.goto('https://workspace.test/uninstall.html');
  await state({});
  assert.ok(await skip.isEnabled()&&await next.isDisabled()&&!(await box.isChecked()));
  const enabled=await colors(skip),disabled=await colors(next);
  assert.notDeepEqual(enabled,disabled);
  const bounds=await option.evaluate(n=>{
   const rect=e=>e.getBoundingClientRect(),input=rect(n.querySelector('input')),text=rect(n.querySelector('span')),line=Number.parseFloat(getComputedStyle(n).lineHeight);
   return {left:input.left-rect(n).left,gap:text.left-input.right,
    centered:Math.abs((input.top+input.bottom-text.top-text.bottom)/2),lines:text.height/line,
    fits:document.documentElement.scrollWidth<=innerWidth};
  });
  assert.ok(bounds.fits&&bounds.left<1&&bounds.gap>=4&&bounds.centered<=1&&bounds.lines<=1.1,JSON.stringify(bounds));
  await option.locator('span').click();assert.ok(await box.isChecked());
  await skip.click();assert.ok(await next.isDisabled()&&await skip.isDisabled());
  await state({backup:'skipped',continue_refusal:''});
  assert.ok(await next.isEnabled()&&await skip.isDisabled()&&await box.isChecked());
  assert.deepEqual(await colors(next),enabled);assert.deepEqual(await colors(skip),disabled);
  await next.click();await p.locator('#uninstall-confirm-text').filter({hasText:'个人资料及目录内的本机备份将被清空'}).waitFor();
  await p.locator('#uninstall-confirm-no').click();
  await state({can_clear_profile:false});
  assert.ok(await box.isDisabled()&&!(await box.isChecked()));
  assert.deepEqual(await p.evaluate(()=>requests.map(r=>r.action)),['uninstall-status','uninstall-skip']);
 }
 await p.setViewportSize({width:1250,height:870});
}

async function main(){
 const browser=await chromium.launch({headless:true,...(process.env.QIBAN_CHROME?{executablePath:process.env.QIBAN_CHROME}:{})});
 const generated=await fs.mkdtemp(path.join(os.tmpdir(),'qiban-public-pages-'));
 try{
  const publicPages=path.join(generated,'public-content.js');
  execFileSync(process.env.QIBAN_DESKTOP_PYTHON||'python',[path.join(__dirname,'build_public_pages.py'),publicPages]);
  const p=await browser.newPage({viewport:{width:1250,height:870}});
  const errors=[];p.on('pageerror',e=>errors.push(e.message));
  await p.addInitScript(bridgeFixture,{operations:supported});
  await p.route('https://workspace.test/**',async route=>{
   const url=new URL(route.request().url()),name=path.basename(url.pathname);
   if(name==='favicon.ico'){await route.fulfill({status:204});return;}
   const source=name==='public-content.js'?publicPages:
    url.pathname.startsWith('/room-stills/')?path.join(assets,'room-stills',name):path.join(assets,name||'index.html');
   try{
    if(name==='index.html')await route.fulfill({path:source,headers:{'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'"}});
    else await route.fulfill({path:source});
   }catch{await route.fulfill({status:404});}
  });
  await checkUninstall(p);
  await checkPublicReader(p);
  await p.goto('https://workspace.test/index.html');
  await checkHouse(p);
  await p.goto('https://workspace.test/index.html');
  const click=k=>p.locator('[data-ws="'+k+'"]').click(),nav=k=>p.locator('[data-nav-target="'+k+'"]').click();
  const reqs=op=>p.evaluate(op=>requests.filter(r=>r.operation===op).map(r=>r.arguments),op);
  await p.evaluate(()=>access(true));await nav('learn');
  await p.getByText('正在整理学习树…',{exact:false}).first().waitFor();
  await p.evaluate(()=>{const refresh=document.querySelector('[data-ws=refresh]');for(let i=0;i<6;i++)refresh.click();});
  await p.locator('[data-item=math]').waitFor();
  assert.equal((await reqs('learning.snapshot')).length,2);
  await p.locator('.ws-cluster').first().waitFor();
  await p.locator('[data-zoom=in]').click();await p.locator('[data-zoom=out]').click();
  const mini=await p.locator('.ws-mini').boundingBox();
  await p.mouse.move(mini.x+25,mini.y+25);await p.mouse.down();await p.mouse.move(mini.x+40,mini.y+40,{steps:5});await p.mouse.up();
  await p.locator('[data-item=math]').click();
  await p.locator('[data-node-form]').waitFor();
  assert.equal(await p.locator('[name=title]').count(),0);
  await click('new'); await p.locator('[name=title]').fill('龙娘的学习笔记');await p.locator('[name=body]').fill('小实验');
  await p.evaluate(()=>{window.conflict=true;});await p.getByRole('button',{name:'保存节点'}).click();
  await p.getByText('当前快照尚无这条笔记',{exact:false}).waitFor();
  assert.equal(await p.locator('[name=title]').inputValue(),'龙娘的学习笔记');
  await p.getByRole('button',{name:'保存节点'}).click();await p.locator('[data-editor] h3').filter({hasText:'龙娘的学习笔记'}).waitFor();
  const edits=await reqs('learning.edit');assert.match(edits[0].node,/^note-[0-9a-f]{32}$/);assert.equal(edits[0].node,edits[1].node);
  assert.equal(edits[0].expected,null);assert.equal(edits[1].definition,'D1');assert.notEqual(edits[0].snapshot,edits[1].snapshot);
  await p.locator('[name=body]').fill('再次编辑');await p.getByRole('button',{name:'保存节点'}).click();
  await p.waitForFunction(()=>requests.filter(r=>r.operation==='learning.edit').length===3);
  const change=(await reqs('learning.edit')).at(-1);assert.ok(change.expected);
  await p.locator('[data-editor] h3').filter({hasText:'龙娘的学习笔记'}).waitFor();
  await p.evaluate(()=>{window.indexPaused=true;});
  await p.locator('[data-goals]').selectOption('second');await p.getByRole('button',{name:'继续整理'}).waitFor();
  const pausedCount=(await reqs('learning.snapshot')).length;
  await new Promise(resolve=>setTimeout(resolve,850));assert.equal((await reqs('learning.snapshot')).length,pausedCount);
  await p.getByRole('button',{name:'继续整理'}).click();
  await p.waitForFunction(()=>requests.some(r=>r.operation==='learning.search'&&r.arguments.definition==='D2'));
  await p.evaluate(()=>{window.holdDetail=true;});await p.locator('[data-item=math]').click();
  await p.waitForFunction(()=>!!window.releaseDetail);await p.evaluate(()=>{access(false);releaseDetail();});await p.getByText('解锁桌面管理后可查看。').waitFor();
  assert.equal(await p.locator('[data-editor]').count(),0);
  await nav('context');await p.evaluate(()=>access(true));
  await p.locator('[data-field=guidance]').waitFor();assert.ok(await p.locator('[data-field=guidance]').isDisabled());
  assert.ok(await p.locator('[data-ws=preset-save]').isDisabled());assert.ok(await p.locator('[data-ws=preview]').isDisabled());
  assert.equal(await p.locator('[data-ws=publish],[data-ws=save],[data-ws=history]').count(),0);
  await p.evaluate(()=>{access(false);window.allowed=['learning.goals'];access(true);});
  await p.getByText('当前后台尚未提供此功能。').waitFor();await nav('learn');await p.getByText('当前后台仅提供目标目录。').waitFor();
  assert.equal(await p.locator('[data-ws=new]').count(),0);
  await p.goto('https://workspace.test/index.html');
  await p.evaluate(()=>desktopService(true));
  await p.waitForFunction(()=>{const scene=document.getElementById('monitor-window');return scene?.base?.naturalWidth>0&&scene.actor.firstChild?.naturalWidth>0;});
  await p.setViewportSize({width:360,height:720});
  await p.locator('#house-preset-line').getByText('默认').waitFor();
  const roomBounds=await p.locator('#monitor-window').boundingBox();
  assert.ok(roomBounds.width>=300&&Math.abs(roomBounds.width/roomBounds.height-16/9)<.04);
  assert.deepEqual(errors,[]);
  console.log('通过：离线支持页与模型简表、微信结果、键盘返回、窄屏、会客室草稿保留、监控场景、模型档位、学习树与上下文工作台。');
 }finally{await browser.close();await fs.rm(generated,{recursive:true,force:true});}
}
module.exports={bridgeFixture,supported};
if(require.main===module)main().catch(error=>{console.error(error);process.exitCode=1;});
