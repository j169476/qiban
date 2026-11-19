// Exercise the same local scene component and assets served by the desktop.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs/promises');
const assets=path.join(__dirname,'Assets');

async function run(){
 const browser=await chromium.launch({headless:true,...(process.env.QIBAN_CHROME?{executablePath:process.env.QIBAN_CHROME}:{})});
 try{
  const context=await browser.newContext({viewport:{width:960,height:820},timezoneId:'Asia/Shanghai'});
  const url='https://room.test/';
  await context.route(url+'**',async route=>{
   const file=new URL(route.request().url()).pathname;
   if(file==='/')return route.fulfill({contentType:'text/html; charset=utf-8',body:
    '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><script defer src="/room-assets.js"></script><script defer src="/room-player.js"></script><qiban-room data-preview></qiban-room>'});
   if(['/room-assets.js','/room-player.js','/room-scene.css'].includes(file)||/^\/room-stills\/[a-z-]+\.webp$/.test(file)){
    return route.fulfill({path:path.join(assets,file.slice(1))});
   }
   return route.fulfill({status:404});
  });
  const page=await context.newPage(),errors=[],requests=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>requests.push(r.url()));
  await page.addInitScript(()=>{Math.random=()=>.02;});
  await page.clock.setFixedTime(new Date('2026-11-20T10:30:00+08:00'));
  await page.goto(url);
  const room=page.locator('qiban-room'),action=k=>room.evaluate((w,activity)=>w.setActivity(activity),k);
  const selectPeriod=id=>room.evaluate((w,id)=>{
   if(id===null)w.removeAttribute('preview-hour');
   else w.setAttribute('preview-hour',String(w.media.periods.find(p=>p.id===id).start/60));
  },id);
  const wait=(pose,period,phase='still')=>page.waitForFunction(({pose,period,phase})=>{
   const w=document.querySelector('qiban-room');
   return (!pose||w.dataset.pose===pose)&&(!period||w.dataset.period===period)&&w.dataset.phase===phase&&w.base.naturalWidth&&w.actor.firstChild?.naturalWidth;
  },{pose,period,phase},{timeout:9000});
  const untilPhase=phase=>page.waitForFunction(phase=>document.querySelector('qiban-room').dataset.phase===phase,phase);
  await wait('sleeping','forenoon');
  await room.evaluate(w=>{
   window.roomChanges=[];
   new MutationObserver(()=>{
    window.roomChanges.push({phase:w.dataset.phase,pose:w.dataset.pose,time:performance.now(),opacity:Number(getComputedStyle(w.shade).opacity)});
   }).observe(w,{attributes:true,attributeFilter:['data-phase','data-pose']});
  });
  await action('learning');await untilPhase('fade-out');
  await wait('sleeping','forenoon','fade-out');
  await untilPhase('black');assert.equal(await room.evaluate(w=>getComputedStyle(w.shade).opacity),'1');
  await wait('learning','forenoon');
  const log=await page.evaluate(()=>window.roomChanges);
  const stamp=phase=>log.find(e=>e.phase===phase).time;
  const ms=stamp('still')-stamp('fade-out');
  assert.ok(ms>=2250&&ms<=2850,'ordinary switch measured '+ms+' ms');
  assert.ok(log.filter(e=>e.pose==='learning')[0].opacity>=.985,'new still must first appear under black');
  assert.equal(await room.evaluate(w=>w.shade.getAnimations().length),0);

  await action('reading');await untilPhase('fade-out');
  await action('replying');await selectPeriod('night');
  await wait('replying','night');
  const gen=await room.evaluate(w=>w.generation);
  await room.evaluate(w=>{w.update();w.update();});
  assert.equal(await room.evaluate(w=>w.generation),gen);
  await action('watching');await untilPhase('fade-out');
  await action('replying');await wait('replying','night');
  // Selecting a different ready target under the black frame replaces the earlier request.
  await action('window');await untilPhase('black');await action('reading');await wait('reading','night');

  await action('watching');await untilPhase('fade-in');await action('learning');
  await page.emulateMedia({reducedMotion:'reduce'});await wait('learning','night');
  assert.equal(await room.evaluate(w=>getComputedStyle(w.shade).opacity),'0');
  const steady=await room.screenshot();await page.waitForTimeout(350);
  assert.deepEqual(await room.screenshot(),steady,'人物和房间应当持续静止');
  const periodList=await room.evaluate(w=>w.media.periods);
  assert.equal(periodList.length,8);
  const folder=process.env.QIBAN_UI_CAPTURES;
  if(folder)await fs.mkdir(folder,{recursive:true});
  await page.setViewportSize({width:676,height:720});
  const samplePoses=['sleeping','window','reading','replying','learning','watching','window','reading'];
  for(const [i,period] of periodList.entries()){
   const pose=samplePoses[i];
   await selectPeriod(period.id);await action(pose);await wait(pose,period.id);
   assert.equal(await room.evaluate(w=>w.shade.getAnimations().length),0);
   if(folder)await room.screenshot({path:path.join(folder,'time-'+period.id+'.png')});
  }
  for(const pose of ['sleeping','reading','window','replying','learning','watching']){
   await action(pose);await wait(pose,'night');
   assert.ok(await room.evaluate(w=>w.actor.firstChild.src.endsWith('-still.webp')&&getComputedStyle(w.actor.firstChild).imageRendering==='pixelated'));
  }
  await selectPeriod(null);await action('idle');await wait('sleeping','forenoon');
  const due=await room.evaluate(w=>w.idleDeadline);const original=await room.locator('.room-actor img').elementHandle();
  assert.ok(await room.evaluate(w=>w.idleDeadline-Date.now()>=180000&&w.idleDeadline-Date.now()<=300000));
  await page.clock.setFixedTime(new Date(due+1));await room.evaluate(w=>w.update());await wait('sleeping','forenoon');
  assert.ok(await original.evaluate(img=>img.isConnected));assert.ok(await room.evaluate((w,old)=>w.idleDeadline>old,due));
  await room.evaluate(w=>{Math.random=()=>.55;w.toggleAttribute('quiet',true);});
  const remaining=await room.evaluate(w=>w.idleRemaining);
  await page.clock.setFixedTime(new Date(due+90000));await room.evaluate(w=>w.toggleAttribute('quiet',false));
  assert.equal(await room.evaluate(w=>w.idleDeadline-Date.now()),remaining);
  const due2=await room.evaluate(w=>w.idleDeadline);
  await page.clock.setFixedTime(new Date(due2+1));await room.evaluate(w=>w.update());await wait('reading','forenoon');

  // Check every boundary and the minute scheduler that crosses midnight while the page stays open.
  for(let i=0;i<periodList.length;i++){
   const p=periodList[i],prior=periodList[(i+7)%8];
   const start=Date.parse('2026-11-21T00:00:00+08:00')+p.start*60000;
   for(const [time,id] of [[start-1,prior.id],[start,p.id]]){
    await page.clock.setFixedTime(new Date(time));await room.evaluate(w=>w.update());await wait(null,id);
   }
  }
  const clockPage=await context.newPage();await clockPage.emulateMedia({reducedMotion:'reduce'});
  await clockPage.clock.install({time:new Date('2026-11-20T23:59:55+08:00')});await clockPage.goto(url);
  await clockPage.waitForFunction(()=>document.querySelector('qiban-room').dataset.period==='night');
  await clockPage.clock.fastForward(5100);
  await clockPage.waitForFunction(()=>document.querySelector('qiban-room').dataset.period==='midnight');
  await clockPage.close();
  await page.setViewportSize({width:360,height:720});
  assert.ok(await room.evaluate(w=>w.stage.clientWidth>=320&&Math.abs(w.stage.clientWidth/w.stage.clientHeight-16/9)<.03));
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.equal(new Set(requests.filter(u=>u.includes('/room-stills/'))).size,14);
  assert.ok(requests.every(u=>!u.includes('/room-art/')&&!/-(enter|exit|loop)\.webp$/.test(u)));
  assert.deepEqual(errors,[]);
  console.log('静态房间通过验收：8 个时段及午夜切换，6 张人物定帧，黑屏转场 '+Math.round(ms)+' ms，连续选择、待机、减少动态效果与窄屏。');
 }finally{await browser.close();}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
