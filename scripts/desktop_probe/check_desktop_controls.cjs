// Chromium runs in its own temporary profile. All application requests use local fixtures.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs/promises');
const {bridgeFixture,supported}=require('./check_workspace_browser.cjs');
const assets=path.join(__dirname,'Assets');
const cases=[[1440,1],[1440,1.25],[1280,1.5],[900,1],[720,1.25],[540,1.5],[360,1]];

function records(){
 const kinds=['comment','reply','dm_reply','dynamic','like','comment_like','favorite','follow','coin','favorite_create','favorite_move'];
 const actions=Object.fromEntries(kinds.map(k=>[k,{enabled:false,daily_limit:2}]));
 window.fixtureReply=(req,ok)=>{
  switch(req.operation){
   case 'qq.status':ok({state:'idle',selected:{uin:'10001',label:'测试账号'},accounts:[],revision:1,directory:{private_targets:[{id:'12345',label:'读书小组的联系人'}],groups:[]}});return true;
   case 'qq.directory':ok({});return true;
   case 'wechat.profiles':ok({accounts:['fixture'],selected:'fixture',revision:'v1'});return true;
   case 'wechat.directory_sync':ok({items:[{id:'wxid_fixture_very_long_name_for_layout',name:'读书小组的联系人',kind:'private'}]});return true;
   case 'wechat.targets.saved':ok({targets:[],version:'v1'});return true;
   case 'media.list':ok({items:[],settings:{days:30},voice:{enabled:false,seconds:30}});return true;
   case 'bili.status':ok({state:'idle',current:{uid:'1',name:'测试账号'}});return true;
   case 'bili.policy':ok({uid:'1',timezone:'Asia/Shanghai',day:'2026-11-21',actions,defaults:actions,used:Object.fromEntries(kinds.map(k=>[k,0]))});return true;
   default:return false;
  }
 };
}
// Measure the first line of visible label text, separately from the native input's box.
function geometry(input){
 if(typeof input==='string'){
  input=document.querySelector(input);
  input.scrollIntoView({block:'center',behavior:'instant'});
 }
 const label=input.labels[0],walker=document.createTreeWalker(label,NodeFilter.SHOW_TEXT);
 let node;do{node=walker.nextNode();}while(node&&!node.textContent.trim());
 const range=document.createRange();range.selectNodeContents(node);const text=range.getClientRects()[0],box=input.getBoundingClientRect();
 return {width:box.width,height:box.height,gap:text.left-box.right,dy:(box.top+box.bottom-text.top-text.bottom)/2,
  x:text.left+Math.min(5,text.width/2),y:(text.top+text.bottom)/2};
}
async function clickText(page,selector){
 const input=page.locator(selector),before=await input.isChecked();
 // Contact directory refreshes can replace a row. Resolve its current label in the same call that measures it.
 const pos=await page.evaluate(geometry,selector);
 assert.ok(pos.width>=12&&pos.width<=24&&pos.gap>=3&&pos.gap<=20,selector+' '+JSON.stringify(pos));
 const caption=input.locator('..').locator('strong');
 if(await caption.count())await caption.click();else await page.mouse.click(pos.x,pos.y);
 assert.equal(await input.isChecked(),!before,selector+' label at '+await page.evaluate(()=>innerWidth));
}
async function visibleTip(tip){
 assert.ok(await tip.isVisible());
 assert.ok(await tip.evaluate(n=>{const r=n.getBoundingClientRect();return r.width>0&&r.left>=-1&&r.right<=innerWidth+1&&r.top>=-1&&r.bottom<=innerHeight+1;}),'help outside viewport');
}
async function navigate(page,name){
 const link=page.locator('[data-nav-target="'+name+'"]');if(!(await link.isVisible()))await page.locator('#house-menu').click();
 await link.click();
}
async function run(){
 const context=await chromium.launchPersistentContext('',{headless:true,viewport:null,...(process.env.QIBAN_CHROME?{executablePath:process.env.QIBAN_CHROME}:{})});
 try{
  await context.route('https://controls.test/**',async route=>{
   const file=new URL(route.request().url()).pathname.replace(/^\/(static\/)?/,'');
   if(file==='public-content.js'||file==='favicon.ico')return route.fulfill({status:204});
   if(file==='examples'){
    // Render the original labels with each section's own styles, including forms normally gated by a login or destructive confirmation.
    const source=await fs.readFile(path.join(assets,'index.html'),'utf8');
    return route.fulfill({contentType:'text/html; charset=utf-8',body:source.replace(/<script\b[^>]*><\/script>/g,'').replace('</head>','<script src="/help.js" defer></script></head>')});
   }
   try{await route.fulfill({path:path.join(assets,file)});}catch{await route.fulfill({status:404});}
  });
  const page=context.pages()[0],settings=await context.newPage(),examples=await context.newPage(),errors=[];
  page.setDefaultTimeout(6500);page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(bridgeFixture,{operations:supported});await page.addInitScript(records);
  await page.goto('https://controls.test/index.html');await page.evaluate(()=>desktopService(true));
  await examples.goto('https://controls.test/examples');await examples.evaluate(()=>{
   for(const n of document.querySelectorAll('main [data-nav-page],main [data-persona-pane],main [data-platform-pane],main [data-media-pane],main [data-records-pane],main [data-backup-pane],main [data-model-pane]')){
    for(const a of [...n.attributes])if(a.name==='data-nav-page'||a.name.endsWith('-pane'))n.removeAttribute(a.name);
   }
   for(const n of document.querySelectorAll('input[type=checkbox],details.help')){
    for(let p=n;p&&p!==document.body;p=p.parentElement){p.hidden=false;if(p.tagName==='DETAILS'&&p!==n)p.open=true;}
    if(n.tagName==='INPUT')n.disabled=false;
   }
   const label=document.createElement('label');label.innerHTML='<input type="radio" name="example">单选示例';
   document.getElementById('media-panel').append(label);
  });
  await settings.goto('chrome://settings/appearance',{waitUntil:'domcontentloaded'});
  const zoom=value=>settings.evaluate(v=>new Promise(resolve=>chrome.settingsPrivate.setDefaultZoom(v,resolve)),value);
  const widths=[];
  for(const [width,factor] of cases){
   await zoom(factor);for(const p of [page,examples])await p.setViewportSize({width,height:880});
   await page.bringToFront();await page.waitForFunction(f=>Math.abs(devicePixelRatio-f)<.03,factor);
   widths.push(await page.evaluate(()=>innerWidth));
   await navigate(page,'media');const box=page.locator('#media-voice-enabled');
   await page.waitForFunction(()=>!document.getElementById('media-voice-enabled').disabled);
   await box.scrollIntoViewIfNeeded();const g=await box.evaluate(geometry);
   assert.ok(g.width>=12&&g.width<=24&&g.gap>=3&&g.gap<20&&Math.abs(g.dy)<=5,JSON.stringify({width,factor,g}));
   await clickText(page,'#media-voice-enabled');
   const summary=page.locator('summary[aria-label="QQ 语音回复说明"]'),tip=summary.locator('..').locator('.help-body');
   await summary.focus();await visibleTip(tip);assert.equal(await tip.textContent(),'默认最长 30 秒，失败则发送文字。');
   await page.keyboard.press('Escape');assert.ok(!(await tip.isVisible()));assert.ok(await summary.evaluate(n=>n===document.activeElement));
   await page.mouse.move(0,0);await summary.hover();await visibleTip(tip);
   await page.mouse.move(0,0);await box.focus();
   if(process.env.QIBAN_UI_CAPTURES&&[1440,360].includes(width)&&factor===1){
    await fs.mkdir(process.env.QIBAN_UI_CAPTURES,{recursive:true});
    const clip=await box.evaluate(n=>{
     const heading=n.labels[0].previousElementSibling,button=document.getElementById('media-voice-save');
     const a=heading.getBoundingClientRect(),b=button.getBoundingClientRect();
     return {x:Math.floor(a.left-4),y:Math.floor(a.top-4),width:Math.ceil(Math.min(a.width,530)+8),height:Math.ceil(b.bottom-a.top+12)};
    });
    await page.screenshot({clip,path:path.join(process.env.QIBAN_UI_CAPTURES,'voice-'+width+'.png')});
   }
   await navigate(page,'platforms');
   for(const [platform,id] of [['qq','qq-contact-picker'],['wechat','audience-contact-picker']]){
    await page.locator('[data-tab-target='+platform+']').click();
    const selector='#'+id+' .contact-list input';await page.waitForFunction(sel=>document.querySelector(sel)?.disabled===false,selector);
    await clickText(page,selector);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),platform+' overflow');
   }
   await page.locator('[data-tab-target=bilibili]').click();
   const policy=page.locator('#bili-policy');if(!await policy.evaluate(n=>n.open))await policy.locator('summary').click();
   const bili=page.locator('#bili-action-comment');await bili.waitFor();const prev=await bili.isChecked();
   await page.getByText('发表评论',{exact:true}).click();assert.equal(await bili.isChecked(),!prev);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'horizontal overflow');
   await examples.bringToFront();
   const inputs=examples.locator('input:is([type=checkbox],[type=radio])');
   for(const input of await inputs.all()){
    await input.evaluate(n=>n.scrollIntoView({block:'center',behavior:'instant'}));const g=await input.evaluate(geometry);
    assert.ok(g.width>=12&&g.width<=24&&g.gap>=3&&g.gap<=20,JSON.stringify({width,factor,id:await input.getAttribute('id'),g}));
    if(await input.getAttribute('type')==='radio'&&await input.isChecked())continue;
    const checked=await input.isChecked();await examples.mouse.click(g.x,g.y);assert.equal(await input.isChecked(),!checked);
   }
   const tips=examples.locator('details.help');
   for(const detail of await tips.all()){
    const summary=detail.locator('summary');await summary.evaluate(n=>n.scrollIntoView({block:'center',behavior:'instant'}));
    await summary.focus();await visibleTip(detail.locator('.help-body'));await examples.keyboard.press('Escape');
   }
  }
  assert.deepEqual(errors,[]);
  console.log(cases.map(([w,z],i)=>w+'px × '+z*100+'% ('+widths[i]+' CSS px)').join(', '));
  console.log('Validated '+await examples.locator('details.help').count()+' help entries, choice labels and the QQ/Bilibili forms.');
 }finally{await context.close();}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
