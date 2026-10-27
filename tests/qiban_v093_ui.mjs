// Synthetic v0.9.3 acceptance: no platform requests and no real user records.
import assert from 'node:assert/strict';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {createRequire} from 'node:module';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'),require=createRequire(import.meta.url);
const {chromium}=require(process.env.QIBAN_PLAYWRIGHT_MODULE||'playwright');
const python=process.env.QIBAN_PYTHON||path.join(root,'.venv','Scripts/python.exe');
const output=process.env.QIBAN_UI_OUTPUT||path.join(root,'.dev/v093/browser');await mkdir(output,{recursive:true});
const rendered=spawnSync(python,['-c','import json; from jinja2 import Environment,FileSystemLoader; e=Environment(loader=FileSystemLoader("src/companion_service/templates")); t={k:e.get_template("index.html").render(kind="role" if k=="role" else "admin",authenticated=k!="login",configured=True,csrf="test",counterpart="http://qiban.test:9002/role",kinds=["persona","memories","policy"]) for k in ("admin","role","login")};t["manual"]=e.get_template("manual.html").render();print(json.dumps(t))'],{cwd:root,encoding:'utf8'});
assert.equal(rendered.status,0,rendered.stderr);const templates=JSON.parse(rendered.stdout);
const state={status:'paused',reason:'explicit_start_required',owner:'thread-unique-id',owner_title:'My management chat',host:{available:true},platforms:[{platform:'qq',available:true,status:'paused',reason:'explicit_start_required'}],quotas:{local:{used:1,limit:20,remaining:19}},backup:{},compression:{}};
const monitor={timezone:'Asia/Shanghai',quotas:[],coding_plan:{status:'unavailable',buckets:[]},tokens:{input_tokens:100,output_tokens:30,cached_input_tokens:0,reasoning_output_tokens:0},token_known_calls:{input_tokens:1,output_tokens:1,cached_input_tokens:1,reasoning_output_tokens:1},days:1,model_calls:1,recent:[{started:'2026-10-27T02:00:00Z',stage:'routing.recall.model.semantic_expansion',status:'completed',duration_ms:0,input_tokens:null,output_tokens:null},{started:'2026-10-27T02:00:01Z',stage:'unclassified',status:'completed',duration_ms:22000,input_tokens:100,output_tokens:30}],tasks:[]};
const privateObject={scope_id:'private-a',scope:{platform:'qq',account_identity:'10000',kind:'private',target_id:'10001'},platform:'qq',label:'Alice',account_label:'Qiban',version:'v1',inherited:true,settings:{private_daily_replies:200},usage:{confirmed:2,reserved:1,uncertain:1}};
const groupObject={scope_id:'group-a',scope:{platform:'qq',account_identity:'10000',kind:'group',target_id:'20001'},platform:'qq',label:'Study group',version:'g1',inherited:false,settings:{group_session_replies:5,group_session_decisions:20,group_session_minutes:10},active:true,used:2,decisions:3};
const bounds={private_daily_replies:[0,10000],group_session_replies:[0,100],group_session_decisions:[1,200],group_session_minutes:[1,120]};
const mutations=[],errors=[];
const browser=await chromium.launch({headless:true,...(process.env.QIBAN_BROWSER?{executablePath:process.env.QIBAN_BROWSER}:{})});
async function context(locale){
 const ctx=await browser.newContext({locale,viewport:{width:1440,height:1050},reducedMotion:'reduce'});
 ctx.on('page',p=>p.on('pageerror',e=>errors.push(String(e))));
 await ctx.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url()),p=url.pathname;let body,type='application/json';
  if(p.startsWith('/static/')){assert.match(p,/^\/static\/[\w.-]+$/);body=await readFile(path.join(root,'src/companion_service',p));type=({'.css':'text/css','.js':'text/javascript','.svg':'image/svg+xml','.png':'image/png'})[path.extname(p)];}
  else if(p==='/status')body=state;
  else if(p==='/operate'){
   const data=req.postDataJSON(),a=data.arguments;
   if(data.operation==='runtime_monitor')body=monitor;
   else if(data.operation==='reply_policy_list')body={items:[],contacts:[privateObject],groups:[groupObject],bounds};
   else if(data.operation==='reply_policy_object_set'||data.operation==='reply_policy_object_reset'){
    mutations.push(data);const item=a.scope_id==='private-a'?privateObject:groupObject;assert.equal(a.expected_version,item.version);
    item.inherited=data.operation.endsWith('reset');item.settings=item.inherited?{private_daily_replies:200}:{...item.settings,...a.changes};item.version+='1';body={role_started:false,platform_paused:false};
   }else if(data.operation==='public_growth_preview')body={token:'preview-token',reason:'public_growth_source_changed',data:{notes:[]}};
   else if(data.operation==='public_growth_rebuild'){mutations.push(data);body={rebuilt:true,archive:'recovery-original',requires_explicit_start:true};}
   else throw new Error('Unexpected operation '+data.operation);
  }else if(p==='/tasks')body=[];
  else if(p==='/conversation')body=req.method()==='GET'?[]:{messages:[{id:'message-a',version:'1',speaker:'user',at:'2026-10-27T02:00:00Z',text:'已暂停 — keep my original text',attachments:[]}],removed:[],updates:[],next:null,newest:'message-a'};
  else{type='text/html';body=templates[p==='/manual'?'manual':p==='/role'?'role':p==='/login'?'login':'admin'];}
  await route.fulfill({status:200,contentType:type,body:typeof body==='object'&&!Buffer.isBuffer(body)?JSON.stringify(body):body});
 });return ctx;
}
try{
 const ctx=await context('fr-FR'),page=await ctx.newPage();
 await page.goto('http://qiban.test:9001/');await page.waitForFunction(()=>document.querySelector('#reply-usage .quota-gear'));
 assert.equal(await page.locator('html').getAttribute('lang'),'en-US');
 assert.equal(await page.locator('[data-language]').inputValue(),'en');
 for(const panel of ['dashboard','platforms','memories','tasks','usage','settings']){
  await page.evaluate(id=>window.qibanNavigate(id),panel);
  const untranslated=await page.locator('#panel-'+panel).evaluate(root=>{
   const nodes=[],walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
   while(walker.nextNode()){const node=walker.currentNode;if(node.parentElement.closest('pre,code,textarea'))continue;
    if(node.parentElement.getClientRects().length&&/[\p{Script=Han}]/u.test(node.textContent))nodes.push(node.textContent.trim());}
   return nodes;
  });assert.deepEqual(untranslated,[],'English panel '+panel);
 }
 await page.evaluate(()=>window.qibanNavigate('usage'));
 const contrastFailures=await page.evaluate(()=>{
  const rgb=value=>(value.match(/[\d.]+/g)||[]).map(Number);
  const blend=(front,back)=>front.slice(0,3).map((v,i)=>v*(front[3]??1)+back[i]*(1-(front[3]??1)));
  const luminance=value=>value.map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
  const failures=[];
  for(const element of document.querySelectorAll('.sidebar nav a,.sidebar-room,.sidebar-bottom footer,.text-link,.object-uid,.badge,th,td,button:not(:disabled),.footer-meta')){
   if(!element.getClientRects().length)continue;
   const parents=[];for(let p=element;p;p=p.parentElement)parents.unshift(p);
   let background=[250,248,252];for(const p of parents)background=blend(rgb(getComputedStyle(p).backgroundColor),background);
   const color=blend(rgb(getComputedStyle(element).color),background),light=[luminance(color),luminance(background)].sort((a,b)=>a-b);
   const ratio=(light[1]+.05)/(light[0]+.05);
   if(ratio<4.5)failures.push({text:element.textContent.slice(0,45),ratio});
  }return failures;
 });assert.deepEqual(contrastFailures,[],'Text contrast on quota and navigation surfaces');
 assert.match(await page.locator('#observation-rows').innerText(),/Recall query expansion record/);
 assert.match(await page.locator('#observation-rows').innerText(),/Unlabelled stage/);
 assert.doesNotMatch(await page.locator('#observation-rows').innerText(),/routing\.recall/);
 const gear=page.locator('[data-scope-id="private-a"] .quota-gear');await gear.click();
 await page.locator('dialog input').fill('250');await page.locator('dialog button[type=submit]').click();
 await page.waitForFunction(()=>!document.querySelector('dialog'));assert.equal(privateObject.settings.private_daily_replies,250);
 assert.equal(privateObject.usage.confirmed,2);assert.equal(state.status,'paused');
 await gear.click();await page.locator('dialog button.secondary').click();await page.waitForFunction(()=>!document.querySelector('dialog'));assert.equal(privateObject.inherited,true);
 await page.locator('[data-scope-id="group-a"] .quota-gear').click();assert.equal(await page.locator('dialog input').count(),3);await page.keyboard.press('Escape');
 for(const width of [1440,768,390,320]){
  await page.setViewportSize({width,height:1050});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,'usage overflow '+width);
  await page.screenshot({path:path.join(output,`v093-en-usage-${width}.png`),fullPage:true});
 }
 await page.setViewportSize({width:1440,height:1050});await page.evaluate(()=>window.qibanNavigate('settings'));
 await page.locator('#growth-preview').click();await page.locator('#growth-rebuild').waitFor({state:'visible'});
 page.once('dialog',d=>d.accept());await page.locator('#growth-rebuild').click();await page.waitForFunction(()=>document.querySelector('#growth-status').textContent.includes('archived'));
 assert.equal(mutations.at(-1).arguments.token,'preview-token');
 await Promise.all([page.waitForEvent('load'),page.locator('[data-language]').selectOption('zh')]);
 assert.equal(await page.locator('html').getAttribute('lang'),'zh-CN');await page.reload();assert.equal(await page.locator('[data-language]').inputValue(),'zh');
 await page.evaluate(()=>window.qibanNavigate('usage'));await page.waitForFunction(()=>document.querySelector('#reply-usage .quota-gear'));await page.screenshot({path:path.join(output,'v093-zh-usage.png'),fullPage:true});
 const other=await ctx.newPage();await other.goto('http://qiban.test:9002/role');assert.equal(await other.locator('[data-language]').inputValue(),'zh');
 await Promise.all([other.waitForEvent('load'),other.locator('[data-language]').selectOption('en')]);await other.waitForFunction(()=>document.querySelector('.bubble'));
 assert.match(await other.locator('.bubble').textContent(),/已暂停 — keep my original text/);
 await other.goto('http://qiban.test:9002/manual');assert.equal(await other.locator('[data-manual-language=en]').isVisible(),true);assert.equal(await other.locator('[data-manual-language=zh]').isVisible(),false);
 await other.setViewportSize({width:320,height:900});assert.equal(await other.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,'manual overflow');
 await other.screenshot({path:path.join(output,'v093-en-manual-mobile.png')});
 await other.goto('http://qiban.test:9002/login');assert.equal(await other.locator('html').getAttribute('lang'),'en-US');
 const zh=await context('zh-TW'),independent=await zh.newPage();await independent.goto('http://qiban.test:9001/');assert.equal(await independent.locator('html').getAttribute('lang'),'zh-CN');
 await independent.evaluate(()=>window.qibanNavigate('dashboard'));await independent.screenshot({path:path.join(output,'v093-zh-dashboard.png'),fullPage:true});
 // 200% text zoom must retain document flow and accessible controls.
 await independent.evaluate(()=>document.documentElement.style.fontSize='200%');await independent.setViewportSize({width:768,height:1050});assert.equal(await independent.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,JSON.stringify(await independent.evaluate(()=>[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).slice(0,12).map(e=>({tag:e.tagName,id:e.id,class:e.className,right:e.getBoundingClientRect().right})))));
 assert.deepEqual(errors,[]);assert.equal(mutations.length,3);
 await writeFile(path.join(output,'v093-checks.json'),JSON.stringify({ok:true,checks:['browser default','manual preference','reload persistence','cross-port preference','browser independence','original conversation text','quota save and reset','group dialog','growth recovery','responsive widths','200% text zoom'],mutations,errors},null,2));
 console.log(JSON.stringify({ok:true,mutations:mutations.length,errors,output}));
}finally{await browser.close();}
