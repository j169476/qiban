// Synthetic browser QA. No requests reach a real local service or platform.
// QIBAN_PLAYWRIGHT_MODULE may point to an installed Playwright package.
import assert from 'node:assert/strict';
import {readFile, mkdir, writeFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {createRequire} from 'node:module';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.QIBAN_PLAYWRIGHT_MODULE||'playwright');
const python=process.env.QIBAN_PYTHON||path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const output=path.join(root,'.dev/v09/browser');await mkdir(output,{recursive:true});
const render=spawnSync(python,['-c',
  'import json; from jinja2 import Environment, FileSystemLoader; e=Environment(loader=FileSystemLoader("src/companion_service/templates")); print(json.dumps({k:e.get_template("index.html").render(kind="role" if k=="role" else "admin", authenticated=k!="login", configured=True, csrf="synthetic", counterpart="http://qiban.test/role", nonce="synthetic", kinds=["persona","memories","policy","companion"]) for k in ("admin","role","login")}))'],{cwd:root,encoding:'utf8'});
assert.equal(render.status,0,render.stderr);const templates=JSON.parse(render.stdout);
const state={"status":"active","busy":false,"reason":"running","generation":"synthetic-local","owner":"synthetic-manager","host":{"available":true,"status":"online","restart_required":false},"platforms":[{"platform":"bilibili","available":true,"status":"paused","reason":"explicit_start_required","generation":null},{"platform":"wechat","available":true,"status":"active","busy":false,"owner":"synthetic-manager","generation":"synthetic-wechat"},{"platform":"qq","available":false,"status":"paused"}],"quotas":{"model":{"used":42,"limit":200,"remaining":158},"local":{"used":12,"limit":100,"remaining":88}},"backup":{"last_saved_at":1792967000,"recovery_key_exported":true},"compression":{"status":"idle"}};
const monitor={"updated_at":1792967200,"timezone":"Asia/Shanghai","quotas":[{"label":"模型调用","used":42,"limit":200,"remaining":158},{"label":"本机会客室回复","used":12,"limit":100,"remaining":88}],"coding_plan":{"status":"available","updated_at":1792967200,"stale":false,"buckets":[{"id":"codex","name":"Codex","windows":[{"name":"primary","used_percent":27,"remaining_percent":73,"window_minutes":300,"resets_at":1792980000},{"name":"secondary","used_percent":18,"remaining_percent":82,"window_minutes":10080,"resets_at":1793500000}]}]},"tokens":{"input_tokens":126400,"cached_input_tokens":82400,"output_tokens":18600,"reasoning_output_tokens":5200},"token_known_calls":{"input_tokens":42,"cached_input_tokens":42,"output_tokens":42,"reasoning_output_tokens":42},"model_calls":42,"days":1,"write_failures":0,"recent":[],"tasks":[]};
const browser=await chromium.launch({headless:true,...(process.env.QIBAN_BROWSER?{executablePath:process.env.QIBAN_BROWSER}:{})});
const errors=[],checks=[],geometry=[],aboutChecks=[],mutations=[];let mode='admin';
const page=await browser.newPage({viewport:{width:1440,height:1050},reducedMotion:'reduce'});
page.on('pageerror',e=>errors.push(String(e)));
await page.route('**/*',async route=>{
 const request=route.request(),url=new URL(request.url()),p=url.pathname;
 let body,type='application/json';
 if(p==='/status')body=JSON.stringify(state);
 else if(p==='/operate'){
  const data=request.postDataJSON();
  if(data.operation==='runtime_monitor')body=JSON.stringify({...monitor,days:data.arguments.days||1});
  else if(data.operation==='reply_policy_list')body=JSON.stringify({items:[]});
  else if(data.operation==='reply_authorization_list')body=JSON.stringify({items:[],platforms:{wechat:{available:true},qq:{available:false}}});
  else {mutations.push(data);body='{}';}
 }else if(p==='/tasks')body='[]';
 else if(p==='/conversation')body=request.method()==='POST'?JSON.stringify({messages:[],removed:[],updates:[],next:null,newest:null}):'[]';
 else if(p.startsWith('/static/')){
  assert.match(p,new RegExp('^/static/[a-zA-Z0-9._-]+$'));
  body=await readFile(path.join(root,'src/companion_service',p));
  type=({'.css':'text/css','.js':'text/javascript','.svg':'image/svg+xml','.png':'image/png','.txt':'text/plain'})[path.extname(p)];
 }else {body=templates[mode];type='text/html';}
 await route.fulfill({status:200,body,contentType:type,headers:{'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'"}});
});
try{
 await page.goto('http://qiban.test/');await page.waitForFunction(()=>document.querySelector('#metric-plan').textContent==='73%');
 for(const width of [1920,1440,1280,1024,950,800,768,720,640,600,480,390,320]){
  await page.setViewportSize({width,height:1050});
  for(const tab of ['dashboard','platforms','memories','tasks','usage','settings']){
   await page.evaluate(t=>window.qibanNavigate(t),tab);
   const result=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth+1,misses:[...document.querySelectorAll('button')].filter(e=>{const r=e.getBoundingClientRect();return r.width&&r.height&&r.top>=0&&r.bottom<=innerHeight}).filter(e=>{const r=e.getBoundingClientRect();return !e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}).map(e=>e.textContent.trim())}));
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
