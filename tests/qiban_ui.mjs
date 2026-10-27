// Synthetic browser QA. No requests reach a real local service or platform.
// QIBAN_PLAYWRIGHT_MODULE may point to an installed Playwright package.
import assert from 'node:assert/strict';
import {readFile, mkdir, writeFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {createRequire} from 'node:module';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const sourceRoot=process.env.QIBAN_UI_SOURCE_ROOT||root;
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.QIBAN_PLAYWRIGHT_MODULE||'playwright');
const python=process.env.QIBAN_PYTHON||path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const output=process.env.QIBAN_UI_OUTPUT||path.join(root,'.dev/v09/browser');await mkdir(output,{recursive:true});
const render=spawnSync(python,['-c',
  'import json; from jinja2 import Environment, FileSystemLoader; e=Environment(loader=FileSystemLoader("src/companion_service/templates")); print(json.dumps({k:e.get_template("index.html").render(kind="role" if k=="role" else "admin", authenticated=k!="login", configured=True, csrf="synthetic", counterpart="http://qiban.test/role", nonce="synthetic", kinds=["persona","memories","policy","companion"]) for k in ("admin","role","login")}))'],{cwd:sourceRoot,encoding:'utf8'});
assert.equal(render.status,0,render.stderr);const templates=JSON.parse(render.stdout);
const state={"status":"active","busy":false,"reason":"running","generation":"synthetic-local","owner":"synthetic-manager","host":{"available":true,"status":"online","restart_required":false},"platforms":[{"platform":"bilibili","available":true,"status":"paused","reason":"explicit_start_required","generation":null},{"platform":"wechat","available":true,"status":"active","busy":false,"owner":"synthetic-manager","generation":"synthetic-wechat"},{"platform":"qq","available":false,"status":"paused"}],"quotas":{"model":{"used":42,"limit":200,"remaining":158},"local":{"used":12,"limit":100,"remaining":88}},"backup":{"last_saved_at":1792967000,"recovery_key_exported":true},"compression":{"status":"idle"}};
const monitor={"updated_at":1792967200,"timezone":"Asia/Shanghai","quotas":[{"label":"模型调用","used":42,"limit":200,"remaining":158},{"label":"本机会客室回复","used":12,"limit":100,"remaining":88}],"coding_plan":{"status":"available","updated_at":1792967200,"stale":false,"buckets":[{"id":"codex","name":"Codex","windows":[{"name":"primary","used_percent":27,"remaining_percent":73,"window_minutes":300,"resets_at":1792980000},{"name":"secondary","used_percent":18,"remaining_percent":82,"window_minutes":10080,"resets_at":1793500000}]}]},"tokens":{"input_tokens":126400,"cached_input_tokens":82400,"output_tokens":18600,"reasoning_output_tokens":5200},"token_known_calls":{"input_tokens":42,"cached_input_tokens":42,"output_tokens":42,"reasoning_output_tokens":42},"model_calls":42,"days":1,"write_failures":0,"recent":[],"tasks":[]};
const browser=await chromium.launch({headless:true,...(process.env.QIBAN_BROWSER?{executablePath:process.env.QIBAN_BROWSER}:{})});
const errors=[],checks=[],geometry=[],aboutChecks=[],mutations=[];let mode='admin',replyRows=[];
const modelChanges=[],modelChoices={version:null,global:null,overrides:{}};
const setupChanges=[],importChanges=[];let statusHttp=200,apiError=null;
state.platforms.find(p=>p.platform==='qq').management_url='http://127.0.0.1:6099/webui';
const modelSnapshot=()=>({...modelChoices,effective:Object.fromEntries(['global','local','bilibili','qq','wechat'].map(scope=>{
 const choice=modelChoices.overrides[scope]||modelChoices.global;
 return [scope,choice?{...choice,source:modelChoices.overrides[scope]?scope:'global'}:{model:'host-model',effort:'low',source:'codex_config'}];
}))});
const page=await browser.newPage({viewport:{width:1440,height:1050},reducedMotion:'reduce',locale:'zh-CN'});
page.on('pageerror',e=>errors.push(String(e)));
await page.route('**/*',async route=>{
 const request=route.request(),url=new URL(request.url()),p=url.pathname;
 let body,type='application/json';
 if(p==='/status'&&statusHttp===401){await route.fulfill({status:401,body:JSON.stringify({error:'session_expired'}),contentType:type});return;}
 if(p==='/status')body=JSON.stringify(state);
 else if(p==='/records'&&url.searchParams.get('kind')==='policy')body=JSON.stringify({version:'synthetic-policy',data:{timezone:'Asia/Shanghai',active_hours:{start:'09:00',end:'23:00'},daily_model_cycles:200,min_action_interval_seconds:60,actions:{reply:{enabled:true,daily_limit:10}}}});
 else if(p==='/operate'){
  const data=request.postDataJSON();
  if(data.operation==='platform_control'&&apiError){await route.fulfill(apiError);return;}
  if(data.operation==='runtime_monitor')body=JSON.stringify({...monitor,days:data.arguments.days||1});
  else if(data.operation==='platform_setup_status')body=JSON.stringify({platform:data.arguments.platform,configured:true,version:'setup-v1',running_job:false,token_saved:true,login_url:'http://127.0.0.1:6099/webui',configuration:data.arguments.platform==='qq'?{account:'10001',private_targets:['10002'],groups:[],http:'http://127.0.0.1:3000',ws:'ws://127.0.0.1:3001'}:{account_dirs:['C:/synthetic/account'],target_hints:[{kind:'private',hint:'wxid_test'}]},instructions:'合成配置测试，不连接实际账号。'});
  else if(data.operation==='platform_setup_run'){setupChanges.push(data.arguments);body=JSON.stringify({accepted:true});}
  else if(data.operation==='reply_import_preview'){importChanges.push(data);body=JSON.stringify({token:'synthetic-preview',confirmable:true,duplicates_removed:1,items:[{identifier:'10002',status:'ready'}]});}
  else if(data.operation==='reply_import_confirm'){importChanges.push(data);body=JSON.stringify({added:1,already_authorized:0,platform_paused:false});}
  else if(data.operation==='model_catalog')body=JSON.stringify({items:[{model:'first-model',label:'模型 A',efforts:['low','high'],default_effort:'low'}]});
  else if(data.operation==='model_settings_get')body=JSON.stringify(modelSnapshot());
  else if(data.operation==='model_settings_set'){
   modelChanges.push(data.arguments);const a=data.arguments;modelChoices.version='revision-1';
   if(a.scope==='global')modelChoices.global={model:a.model,effort:a.effort};else modelChoices.overrides[a.scope]={model:a.model,effort:a.effort};
   body=JSON.stringify(modelSnapshot());
  }
  else if(data.operation==='reply_policy_list')body=JSON.stringify({items:[]});
  else if(data.operation==='reply_authorization_list'){
   const a=data.arguments,rows=replyRows.filter(r=>r.scope.platform===a.platform&&(!a.query||r.scope.target_id===a.query)&&(a.view!=='authorized'||r.enabled));
   body=JSON.stringify({items:rows.slice(a.offset,a.offset+a.limit),total:rows.length,offset:a.offset,next_offset:a.offset+a.limit<rows.length?a.offset+a.limit:null,platforms:{wechat:{available:true},qq:{available:true}}});
  }
  else {mutations.push(data);body='{}';}
 }else if(p==='/tasks')body='[]';
 else if(p==='/conversation')body=request.method()==='POST'?JSON.stringify({messages:[],removed:[],updates:[],next:null,newest:null}):'[]';
 else if(p.startsWith('/static/')){
  assert.match(p,new RegExp('^/static/[a-zA-Z0-9._-]+$'));
  body=await readFile(path.join(sourceRoot,'src/companion_service',p));
  type=({'.css':'text/css','.js':'text/javascript','.svg':'image/svg+xml','.png':'image/png','.txt':'text/plain'})[path.extname(p)];
 }else {body=templates[mode];type='text/html';}
 await route.fulfill({status:200,body,contentType:type,headers:{'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'"}});
});
try{
 await page.goto('http://qiban.test/');await page.waitForFunction(()=>document.querySelector('#metric-plan').textContent==='73%');
 // Active lifecycle must not conceal connector retries, even while controls have focus.
 const qq=state.platforms.find(p=>p.platform==='qq'),wechat=state.platforms.find(p=>p.platform==='wechat');
 const originalQQ={...qq};Object.assign(qq,{available:true,status:'active',connection:{state:'retrying',code:'qq_disconnected'}});
 wechat.connection={state:'retrying',code:'client_not_running'};
 await page.evaluate(()=>window.qibanNavigate('platforms'));await page.locator('[data-platform="bilibili"] input[type=number]').focus();
 await page.evaluate(()=>refresh());
 for(const platform of ['qq','wechat'])assert.equal(await page.locator(`[data-platform="${platform}"] .badge`).textContent(),'等待重连');
 for(const platform of ['qq','wechat'])assert.equal(await page.locator(`[data-platform="${platform}"] [data-runtime-stop]`).isEnabled(),true);
 assert.match(await page.locator('#activity-summary').textContent(),/连接需要检查/);
 await page.screenshot({path:path.join(output,'platform-connection-retrying.png'),fullPage:true});
 for(const [connection,label] of [[{state:'new',code:''},'准备连接'],[{state:'blocked',code:'source_unavailable'},'连接受阻'],[{state:'blocked',code:'client_restart_required'},'需重启客户端'],[{state:'uncertain',code:'send_unconfirmed'},'发送待核实']]){
  qq.connection=wechat.connection=connection;await page.evaluate(()=>refresh());
  for(const platform of ['qq','wechat'])assert.equal(await page.locator(`[data-platform="${platform}"] .badge`).textContent(),label);
  if(connection.state==='new')assert.match(await page.locator('#activity-summary').textContent(),/正在准备平台连接/);
 }
 const connectorLink=page.locator('[data-platform="qq"] [data-connector-link]');
 assert.equal(await connectorLink.getAttribute('href'),'http://127.0.0.1:6099/webui');
 assert.equal(await connectorLink.getAttribute('target'),'_blank');
 assert.equal(await connectorLink.getAttribute('rel'),'noopener noreferrer');
 qq.management_url='http://127.0.0.1:6209/webui';await page.evaluate(()=>refresh());
 assert.equal(await connectorLink.getAttribute('href'),qq.management_url);
 for(const invalid of ['javascript:alert(1)','http://127.0.0.1:6099/webui?token=secret','http://example.com:6099/webui']){
  qq.management_url=invalid;await page.evaluate(()=>refresh());assert.equal(await connectorLink.isVisible(),false);assert.equal(await connectorLink.getAttribute('href'),null);
 }
 qq.management_url=originalQQ.management_url;await page.evaluate(()=>refresh());
 for(const [status,contentType,body] of [[500,'text/plain','Internal Server Error private-path'],[502,'text/html','<html>private-token</html>'],[503,'text/plain',''],[200,'text/plain','not JSON']]){
  apiError={status,contentType,body};
  const error=await page.evaluate(()=>operate('platform_control',{}).catch(e=>({message:e.message,status:e.httpStatus})));
  assert.equal(error.status,status);assert.match(error.message,new RegExp('HTTP '+status));assert.doesNotMatch(error.message,/private-|Unexpected token|Internal Server Error/);
 }
 apiError={status:403,contentType:'application/json',body:JSON.stringify({error:'bridge_build_changed',message:'微信桥接构建与当前源码不一致，请重新准备连接依赖。'})};
 assert.match(await page.evaluate(()=>operate('platform_control',{}).catch(e=>e.message)),/桥接构建与当前源码不一致/);
 apiError=null;
 qq.connection={state:'connected',code:''};wechat.connection={state:'connected',code:''};await page.evaluate(()=>refresh());
 for(const width of [1440,390,320]){
  await page.setViewportSize({width,height:1050});await connectorLink.scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,JSON.stringify({width,overflow:await page.evaluate(()=>[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).slice(0,12).map(e=>({tag:e.tagName,id:e.id,class:e.className,right:e.getBoundingClientRect().right})))}));
  assert.equal(await connectorLink.evaluate(el=>{const r=el.getBoundingClientRect();return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}),true);
  await page.screenshot({path:path.join(output,`qq-connector-management-link-${width}.png`)});
 }
 await page.context().route('http://127.0.0.1:6099/webui',route=>route.fulfill({body:'Synthetic connector management page',contentType:'text/html'}));
 const [popup]=await Promise.all([page.waitForEvent('popup'),connectorLink.click()]);
 await popup.waitForLoadState();assert.equal(popup.url(),'http://127.0.0.1:6099/webui');await popup.close();
 await page.evaluate(()=>window.qibanNavigate('usage'));await page.locator('#quota-load').click();
 await page.waitForFunction(()=>document.querySelector('#quota-fields input[name=daily_model_cycles]'));
 assert.equal(await page.locator('#quota-fields input[type=time]').count(),0);
 assert.doesNotMatch(await page.locator('#quota').textContent(),/活动|休息/);
 await page.evaluate(()=>window.qibanNavigate('platforms'));
 for(const platform of ['qq','wechat'])assert.equal(await page.locator(`[data-platform="${platform}"] .badge`).textContent(),'运行中');
 statusHttp=401;await page.evaluate(()=>refresh().catch(()=>{}));
 assert.equal(await page.locator('#host-banner-title').textContent(),'管理页登录已过期');
 assert.equal(await page.locator('#host-banner').evaluate(el=>el.classList.contains('disconnected')),false);
 await page.screenshot({path:path.join(output,'session-expired.png'),fullPage:true});
 statusHttp=200;Object.assign(qq,originalQQ);delete qq.connection;delete wechat.connection;
 await page.locator('[data-platform="bilibili"] input[type=number]').blur();await page.evaluate(()=>refresh());
 assert.equal(await page.locator('#host-banner').isVisible(),false);
 // Large synthetic authorization directory: only one page is rendered.
 replyRows=Array.from({length:45},(_,i)=>({id:`scope-${i}`,version:'v1',enabled:true,verified_target:true,label:`测试对象 ${i}`,account_label:'测试QQ',restriction:'',scope:{platform:'qq',account_identity:'10001',kind:'private',target_id:String(20000+i)}}));
 replyRows.push({id:'new-scope',version:null,enabled:false,verified_target:true,label:'<img src=x onerror=bad()>',account_label:'测试QQ',restriction:'',scope:{platform:'qq',account_identity:'10001',kind:'group',target_id:'99999'}});
 await page.evaluate(()=>window.qibanNavigate('platforms'));
 await page.locator('#load-reply-authorizations').click();
 await page.waitForFunction(()=>document.querySelectorAll('#reply-authorizations article').length===20);
 assert.match(await page.locator('#reply-page-label').textContent(),/1–20 \/ 45/);
 await page.locator('#reply-next').click();await page.waitForFunction(()=>document.querySelector('#reply-page-label').textContent==='21–40 / 45');
 await page.locator('#reply-next').click();await page.waitForFunction(()=>document.querySelectorAll('#reply-authorizations article').length===5);
 assert.equal(await page.locator('#reply-next').isVisible(),false);
 await page.locator('#reply-search-query').fill('99999');await page.locator('#reply-search-form button').click();
 await page.waitForFunction(()=>document.querySelectorAll('#reply-authorizations article').length===1);
 assert.match(await page.locator('#reply-authorizations').textContent(),/<img src=x/);
 assert.equal(await page.locator('#reply-authorizations img').count(),0);
 page.once('dialog',dialog=>dialog.dismiss());await page.locator('#reply-authorizations button').click();
 assert.equal(mutations.filter(m=>m.operation==='reply_authorization_set').length,0);
 for(const width of [1440,390,320]){
  await page.setViewportSize({width,height:1050});await page.locator('#reply-search-form').scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.screenshot({path:path.join(output,`reply-search-${width}.png`),fullPage:false});
 }
 await page.locator('#reply-search-platform').selectOption('wechat');
 assert.equal(await page.locator('#reply-authorizations article').count(),0);
 replyRows=[];
 await page.locator('#setup-load').click();await page.waitForFunction(()=>document.querySelector('#setup-qq-account').value==='10001');
 assert.equal(await page.locator('#setup-qq-login').getAttribute('href'),'http://127.0.0.1:6099/webui');
 await page.locator('#setup-qq-private').fill('10002\n10003');await page.locator('#setup-form button[type=submit]').click();
 await page.waitForFunction(()=>!document.querySelector('#setup-form').disabled);
 assert.equal(setupChanges.length,1);assert.deepEqual(setupChanges[0].settings.private_targets,['10002','10003']);
 assert.equal(setupChanges[0].expected_version,'setup-v1');
 await page.locator('#setup-platform').selectOption('wechat');await page.locator('#setup-load').click();
 await page.waitForFunction(()=>document.querySelector('#setup-wechat-existing').textContent.includes('wxid_test'));
 assert.equal(await page.locator('#setup-wechat-private').inputValue(),'');
 assert.equal(await page.locator('#setup-wechat-groups').inputValue(),'');
 await page.locator('#setup-form button[type=submit]').click();
 await page.waitForFunction(()=>document.querySelector('#setup-probe').disabled===false);
 assert.deepEqual(setupChanges.at(-1).settings.targets,[]);
 assert.equal(await page.locator('#setup-qq-fields').isVisible(),false);
 for(const width of [1440,390,320]){
  await page.setViewportSize({width,height:1050});await page.locator('#setup-form').scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.screenshot({path:path.join(output,`platform-setup-${width}.png`)});
 }
 await page.locator('details:has(#reply-import-form) summary').click();
 await page.locator('#reply-import-identifiers').fill('10002\n10002');await page.locator('#reply-import-form button').click();
 await page.locator('#reply-import-confirm').waitFor({state:'visible'});
 assert.equal(importChanges.filter(c=>c.operation==='reply_import_confirm').length,0);
 await page.locator('#reply-import-identifiers').fill('10003');assert.equal(await page.locator('#reply-import-confirm').isVisible(),false);
 await page.locator('#reply-import-form button').click();await page.locator('#reply-import-confirm').click();
 await page.waitForFunction(()=>document.querySelector('#reply-import-summary').textContent.includes('已追加'));
 assert.equal(importChanges.filter(c=>c.operation==='reply_import_confirm').length,1);
 await page.evaluate(()=>window.qibanNavigate('settings'));await page.locator('#model-refresh').click();
 await page.locator('#model-scope').selectOption('qq');await page.locator('#model-choice').selectOption('first-model');
 await page.locator('#model-effort').selectOption('high');await page.locator('#model-settings-form button').click();
 await page.waitForFunction(()=>document.querySelector('#model-state').textContent.includes('已保存'));
 assert.deepEqual(modelChanges,[{scope:'qq',model:'first-model',effort:'high',expected_version:null}]);
 assert.match(await page.locator('#model-effective').textContent(),/first-model/);
 for(const width of [1440,390]){
  await page.setViewportSize({width,height:1050});await page.locator('#model-settings-form').scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.screenshot({path:path.join(output,`model-settings-${width}.png`)});
 }
 for(const width of [1920,1440,1280,1024,950,800,768,720,640,600,480,390,320]){
  await page.setViewportSize({width,height:1050});
  for(const tab of ['dashboard','platforms','memories','tasks','usage','settings']){
   await page.evaluate(t=>window.qibanNavigate(t),tab);
   const result=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth+1,misses:[...document.querySelectorAll('button')].filter(e=>{const r=e.getBoundingClientRect();return e.checkVisibility()&&r.width&&r.height&&r.top>=0&&r.bottom<=innerHeight}).filter(e=>{const r=e.getBoundingClientRect();return !e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}).map(e=>e.textContent.trim())}));
   assert.equal(result.overflow,false,JSON.stringify({width,tab,...result}));assert.deepEqual(result.misses,[],JSON.stringify({width,tab,...result}));checks.push({width,tab,...result});
  }
 }
 // Geometry checks catch art/text overlaps even when buttons remain clickable.
 for(const width of [1920,1440,1280,1024,950,800,768,720,640,600,480,390,320]){
  await page.setViewportSize({width,height:900});await page.evaluate(()=>window.qibanNavigate('dashboard'));
  await page.locator('.welcome-art img').evaluate(img=>img.decode());
  const g=await page.evaluate(()=>{
   const box=s=>{const r=document.querySelector(s).getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom};};
   return {card:box('.welcome'),copy:box('.welcome-copy'),art:box('.welcome-art img'),note:box('.art-note')};
  });
  const {card,copy,art,note}=g;
  assert.ok(art.left>=card.left+20 && art.right<=card.right-20 && art.top>=card.top+20 && note.bottom<=card.bottom-20,JSON.stringify({width,g}));
  assert.ok(art.left-copy.right>=24 || art.top-copy.bottom>=24,`Art crowds copy: ${JSON.stringify({width,g})}`);
  assert.ok(note.top-art.bottom>=12,`Caption overlaps art: ${JSON.stringify({width,g})}`);
  geometry.push({view:'dashboard',width,...g});
 }
 await page.setViewportSize({width:1440,height:1050});await page.evaluate(()=>window.qibanNavigate('platforms'));
 const duration=page.locator('[data-platform="bilibili"] input[type=number]');await duration.fill('45');await duration.focus();
 await page.evaluate(s=>window.renderQibanState({...s,busy:true}),state);assert.equal(await duration.inputValue(),'45');
 await page.evaluate(s=>window.renderQibanState({...s,host:{available:false,restart_required:true}}),state);
 assert.equal(await page.locator('[data-runtime-start]:enabled').count(),0);assert.equal(await page.locator('#host-banner-title').textContent(),'ChatGPT已停止运行');
 await page.evaluate(()=>window.qibanConnectionLost());assert.equal(await page.locator('#host-banner-title').textContent(),'暂时无法连接本机服务');
 assert.match(await page.locator('#host-banner-message').textContent(),/无法保存配置/);
 await page.evaluate(s=>window.renderQibanState({...s,host:{available:true,restart_required:true}}),state);
 assert.equal(await page.locator('#host-banner-title').textContent(),'服务等待明确重启');
 assert.equal(await page.locator('[data-runtime-start]:enabled').count(),0);
 await page.evaluate(s=>{document.activeElement.blur();window.renderQibanState(s);document.querySelector("#status").textContent="已启动";window.qibanNavigate('dashboard');},state);
 await page.screenshot({path:path.join(output,'dashboard-desktop.png')});
 await page.setViewportSize({width:390,height:1000});await page.screenshot({path:path.join(output,'dashboard-mobile.png')});
 for(const view of ['login','role']){
  mode=view;await page.goto('http://qiban.test/');
  if(view==='role')await page.evaluate(()=>window.refreshConversation());
  if(view==='login'){
   for(const width of [1920,1440,1024,768,600,390,320]){
    await page.setViewportSize({width,height:900});
    const g=await page.evaluate(()=>{
     const box=s=>{const r=document.querySelector(s).getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom};};
     return {card:box('.login'),art:box('.login-mascot'),copy:box('.login .eyebrow')};
    });
    assert.ok(g.art.top>=g.card.top+20 && g.art.left>=g.card.left+20 && g.art.right<=g.card.right-20 && g.copy.top>=g.art.bottom+16,JSON.stringify({width,g}));
    geometry.push({view,width,...g});
   }
   await page.setViewportSize({width:390,height:1000});
  }

  assert.equal(await page.locator('#notice').textContent(),'');
  await page.screenshot({path:path.join(output,view+'.png')});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,view);
 }
 for(const view of ['admin','login','role']){
  mode=view;await page.goto('http://qiban.test/');
  assert.equal(await page.locator('#project-about').getAttribute('open'),null);
  assert.match(await page.locator('.footer-meta').textContent(),/YiNing和ta的小龙/);
  for(const width of [1440,768,320]){
   await page.setViewportSize({width,height:1000});
   const summary=page.locator('#project-about>summary');await summary.focus();await summary.press('Enter');
   assert.equal(await page.locator('.about-content').isVisible(),true);
   assert.equal(await page.locator('.about-links a[href="https://github.com/j169476/qiban"]').isVisible(),true);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,`${view} about overflow ${width}`);
   if(view==='admin' && width===1440)await page.locator('.page-footer').screenshot({path:path.join(output,'about-desktop.png')});
   await summary.press('Enter');assert.equal(await page.locator('.about-content').isVisible(),false);
   aboutChecks.push({view,width,keyboardToggle:true,overflow:false});
  }
 }
 assert.deepEqual(mutations,[]);assert.deepEqual(errors,[]);
 await writeFile(path.join(output,'checks.json'),JSON.stringify({ok:true,checks,geometry,aboutChecks,errors,mutations},null,2));
 console.log(JSON.stringify({ok:true,layouts:checks.length,geometry:geometry.length,about:aboutChecks.length,errors,mutations,output}));
}finally{await browser.close();}
