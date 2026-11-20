// Run against the embedded desktop pages with synthetic, versioned records.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs/promises');
const {bridgeFixture,supported}=require('./check_workspace_browser.cjs');
const extra=['context.preview_config','context.source_list','context.source_detail','context.source_save','context.source_versions','context.source_version',
 'context.presets','context.preset_read','context.preset_save','context.preset_apply','context.preset_replies'];

function versionedFixture(){
 const layers=['memory_records','summary','relations','growth','catalog','files','stickers'];
 const defaults={guidance:{},limits:{},order:layers},presets=new Map();
 const summaries={summary:{content:'原摘要'},growth:{reflection:'原反思',traits:['安静'],speech_habits:['简短'],opinions:['倾听'],interests:{阅读:0.5}}};
 const past={summary:{content:'最初摘要'},growth:{...summaries.growth,reflection:'最初反思'}};
 const rev={summary:'v1',growth:'v1',files:'v1',stickers:'v1'};
 let config=structuredClone(defaults),revision='initial',counter=1;
 let record={version:'m1',data:{id:'mem',content:'原笔记',tags:['手记']}};
 const summary=(layer,fields=summaries[layer])=>({id:layer,title:'本机会话 · 小林',fields,version:rev[layer]});
 const defaultPlan={id:'default',name:'默认',revision:'initial',config:defaults};
 let activePreset={id:'default',name:'默认',revision:'initial'};
 const workspace=()=>({active:{version:revision,config,preset:activePreset},layers,scenes:['private','group']});
 const libraryItem=layer=>({id:layer,revision:rev[layer],name:layer==='files'?'读书笔记':'晚安',tags:[],scopes:[],enabled:true,bytes:32,frames:1,preview:''});
 const relation={id:'rel',relation:'同学',declared_reason:'一起读书',status:'confirmed',version:'r2'};
 const factRow=(id,value,status)=>({id,attribute:'preferred_name',value,status,valid_from:'2026-01-01',quote:value,source:{reference:'本机'}});
 let relationPreview=null;
 window.fixtureReply=(req,ok,fail)=>{
  const a=req.arguments||req,op=req.operation||req.action;
  const prior={version:'old',at:'2026-11-01T08:00:00Z',current:false};
  function apply(){config=structuredClone(preset.config);revision='r'+(++counter);activePreset={id:preset.id,name:preset.name,revision:preset.revision};window.fixtureActive=activePreset;}
  let preset;
  switch(op){
   case 'context.workspace':ok(workspace());break;
   case 'context.preview_config':if(Object.keys(a).sort().join()!=='config,scene'){fail('management_request');break;}ok({scene:a.scene,source:'synthetic',reports:{active:{},draft:{}}});break;
   case 'context.presets':ok({items:[...presets.values()],after:null});break;
   case 'context.preset_read':ok(a.id==='default'?{id:'default',name:'默认',revision:'initial',config:defaults}:presets.get(a.id));break;
   case 'context.preset_save':
    if(window.conflictPreset){window.conflictPreset=false;const old=presets.get(a.id);old.config.guidance.group='另一窗口修改了群聊';old.revision='other';}
    if(Object.keys(a).sort().join()!=='config,id,name,revision'||a.revision!==(presets.get(a.id)?.revision??null)){fail('edit_conflict');break;}
    preset={id:a.id,name:a.name,revision:'p'+(++counter),config:a.config};presets.set(a.id,preset);ok({preset,workspace:workspace()});break;
   case 'context.preset_apply':
    if(Object.keys(a).sort().join()!=='base,id,revision'||a.base!==revision){fail('edit_conflict');break;}
    preset=a.id==='default'?defaultPlan:presets.get(a.id);if(a.revision!==preset?.revision){fail('edit_conflict');break;}apply();ok({preset,workspace:workspace()});break;
   case 'context.preset_replies':{
    const rows=a.id==='default'?[{id:'d',text:'默认方案的回复'}]:[{id:'s',text:'慢读方案的回复'},{id:'s2',text:'更早的慢读回复'}];
    const filtered=rows.filter(row=>!(window.cleaned||[]).includes(row.id)).map(row=>({...row,at:'2026-11-01T08:00:00Z',platform:'local'}));
    const start=a.after?filtered.findIndex(row=>row.id===a.after)+1:0;
    ok({items:filtered.slice(start,start+1),after:filtered.length>start+1?filtered[start].id:null});break;
   }
   case 'context.source_list':ok({items:[{id:a.layer,title:'本机会话 · 小林',preview:summaries[a.layer].content||summaries[a.layer].reflection}],after:null});break;
   case 'context.source_detail':ok(summary(a.layer));break;
   case 'context.source_versions':ok({items:[{version:rev[a.layer],current:true},prior],original:'old',next:null});break;
   case 'context.source_version':ok(summary(a.layer,a.version==='old'?past[a.layer]:summaries[a.layer]));break;
   case 'context.source_save':
    if(window.conflictSource){window.conflictSource=false;summaries.summary.content='别处的摘要';rev.summary='external';}
    if(a.expected!==rev[a.layer]){fail('edit_conflict');break;}
    summaries[a.layer]=a.fields;rev[a.layer]='v'+(++counter);ok(summary(a.layer));break;
   case 'records.list':ok({records:[{id:'mem'},{id:'fact'}],next:null});break;
   case 'records.read':ok(a.identifier==='fact'?{version:'f2',data:{id:'fact',content:'个人事实',tags:[],fact:{}}}:record);break;
   case 'records.correct':record={version:'m2',data:a.value};ok(record);break;
   case 'records.versions':ok({items:[{version:record.version,current:true},prior],original:'old',next:null});break;
   case 'records.version':ok({version:'old',data:{content:'旧笔记',tags:['旧标签']}});break;
   case 'records.restore_version':record={version:'m3',data:{id:'mem',content:'旧笔记',tags:['旧标签']}};ok(record);break;
   case 'facts.history':ok({revision:'timeline',at:'2026-11-01',total:2,counts:{active:1,disputed:0,historical:1,future:0},items:[factRow('latest','阿林','active'),factRow('old-fact','小林','historical')]});break;
   case 'facts.restore_version':ok({history_preserved:true});break;
   case 'relations.list':ok({claims:[relation],total:1,next:null});break;
   case 'relations.detail':ok({claim:relation,history_available:true,history:{},policy:{},source:{},targets:[{value:'disputed'}]});break;
   case 'relations.history':ok({items:[{sequence:1,at:1800000000,before:{version:'r1',status:'claimed',text:{relation:'朋友',reason:'第一次见面'}},after:{version:'r2',status:'confirmed',text:{relation:'同学',reason:'一起读书'}}}]});break;
   case 'relations.text_preview':case 'relations.restore_preview':
    relationPreview={token:'review',identifier:'rel',before:{status:relation.status,text:{relation:relation.relation,reason:relation.declared_reason}},
     after:{status:'confirmed',text:{relation:op==='relations.text_preview'?a.relation:'朋友',reason:a.declared_reason||'第一次见面'}},reason:a.reason};
    ok(relationPreview);break;
   case 'relations.cancel':ok({cancelled:true});break;
   case 'relations.commit':relation.relation=relationPreview.after.text.relation;ok({...relationPreview,changed:true});break;
   case 'files.list':case 'stickers.list':ok({items:[libraryItem(op.split('.')[0])]});break;
   case 'files.targets':ok({items:[]});break;
   case 'files.configure':case 'stickers.configure':ok({});break;
   case 'library.versions':ok({items:[{...libraryItem(a.layer),current:true},{...libraryItem(a.layer),revision:'old',at:prior.at,name:'旧原件',sha256:'abc',current:false}],after:null});break;
   case 'library.restore':case 'library-replace':rev[a.layer]='v'+(++counter);ok({});break;
   case 'library-export-version':ok({});break;
   default:return false;
  }return true;
 };
}

async function main(){
 const browser=await chromium.launch({headless:true,...(process.env.QIBAN_CHROME?{executablePath:process.env.QIBAN_CHROME}:{})});
 try{
  const p=await browser.newPage({viewport:{width:1250,height:870}}),errors=[];p.setDefaultTimeout(6500);
  p.on('pageerror',error=>errors.push(error.message));
  await p.addInitScript(bridgeFixture,{operations:[...supported,...extra]});await p.addInitScript(versionedFixture);
  await p.route('https://context.test/**',async route=>{
   const pathname=new URL(route.request().url()).pathname;
   if(['/public-content.js','/favicon.ico'].includes(pathname)){await route.fulfill({status:204});return;}
   const name=pathname.startsWith('/room-stills/')?pathname.slice(1):path.basename(pathname);
   try{await route.fulfill({path:path.join(__dirname,'Assets',name)});}catch{await route.fulfill({status:404});}
  });
  await p.goto('https://context.test/index.html');await p.evaluate(()=>desktopService(true));
  const nav=p.locator('[data-nav-target=context]');await nav.click();
  const row=id=>p.locator('[data-source="'+id+'"]'),ws=id=>p.locator('[data-ws="'+id+'"]'),guidance=p.locator('[data-field=guidance]');
  const dialog=()=>p.locator('.ws-source-dialog[open]').last();
  const act=async id=>dialog().locator('[data-do="'+id+'"]').click();
  const req=op=>p.evaluate(op=>requests.filter(r=>(r.operation||r.action)===op),op);
  const waitCalls=(op,count)=>p.waitForFunction(([op,count])=>requests.filter(r=>(r.operation||r.action)===op).length===count,[op,count]);
  const back=async()=>{await p.locator('.ws-source-return').click();await guidance.waitFor();};
  await guidance.waitFor();
  assert.deepEqual(await p.locator('.ws-plan-card [data-ws]').allTextContents(),['应用方案','恢复默认','回复记录']);
  assert.deepEqual(await p.locator('.ws-editor-tools [data-ws]').allTextContents(),['新建','加载','保存','试算草稿']);
  assert.equal(await p.locator('.ws-plan-editor :is(select,input,textarea):not([aria-describedby])').count(),0);
  const hint=p.getByRole('button',{name:'使用场景的说明'}),tip=p.locator('.ws-help').filter({has:hint}).getByRole('tooltip');
  await hint.focus();await tip.waitFor();await p.keyboard.press('Escape');await tip.waitFor({state:'hidden'});await hint.hover();await tip.waitFor();
  if(process.env.QIBAN_UI_CAPTURES){
   await fs.mkdir(process.env.QIBAN_UI_CAPTURES,{recursive:true});await p.locator('[data-preset-select]').focus();await p.mouse.move(1,1);
   await p.locator('[data-preset-select]').evaluate(el=>el.blur());await p.evaluate(()=>scrollTo(0,0));
   await p.screenshot({path:path.join(process.env.QIBAN_UI_CAPTURES,'context-workbench.png')});
  }
  await guidance.fill('保留这份上下文草稿');assert.equal(await p.locator('[data-source]').count(),7);
  await ws('preview').click();await p.getByRole('heading',{name:/合成场景试算/}).waitFor();
  assert.equal((await req('context.preview_config'))[0].arguments.config.guidance.private,'保留这份上下文草稿');
  assert.equal((await req('context.save')).length,0);
  for(const [layer,id] of [['catalog','memory-panel'],['memory_records','memory-panel'],['relations','relation-claims-panel'],['files','file-panel'],['stickers','sticker-panel']]){
   await row(layer).click();assert.ok(await p.locator('#'+id+' .ws-source-return').isVisible());await back();assert.equal(await guidance.inputValue(),'保留这份上下文草稿');
  }
  for(const width of [360,1250]){
   await p.setViewportSize({width,height:800});assert.ok(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   if(width===360&&process.env.QIBAN_UI_CAPTURES){
    await p.evaluate(()=>scrollTo(0,0));await p.screenshot({path:path.join(process.env.QIBAN_UI_CAPTURES,'context-narrow.png')});
   }
   await row('summary').click();await dialog().locator('[data-index="0"]').click();
   await dialog().locator('[name=content]').fill('手动编辑');assert.ok(await dialog().evaluate(d=>d.scrollWidth<=d.clientWidth+1));
   await act('save');await act('no');assert.equal((await req('context.source_save')).length,width===360?0:3);
   if(width===360){
    await p.evaluate(()=>{window.conflictSource=true;});await act('save');await act('yes');await dialog().getByText('别处的摘要').waitFor();
    assert.equal(await dialog().locator('[name=content]').inputValue(),'手动编辑');
    await act('keep');await act('save');await act('yes');await dialog().getByText('已保存。').waitFor();
    assert.equal((await req('context.source_save')).at(-1).arguments.expected,'external');
    await act('versions');await dialog().locator('[data-index="1"]').click();await dialog().getByText('最初摘要').waitFor();
    await act('restore');await act('no');assert.equal((await req('context.source_save')).length,2);
    await act('restore');await act('yes');await p.waitForFunction(()=>document.querySelector('[name=content]')?.value==='最初摘要');
    await act('close');
   }else{await act('close');await act('yes');}
  }
  await row('growth').click();await dialog().locator('[data-index="0"]').click();await dialog().locator('[name=reflection]').fill('今天的反思');
  await dialog().locator('[name=traits]').fill('安静\n好奇');await act('add-interest');await dialog().locator('[name=topic]').last().fill('写作');
  await dialog().locator('[name=score]').last().fill('1.5');await act('save');await dialog().getByText(/权重在 0 到 1/).waitFor();
  await dialog().locator('[name=score]').last().fill('0.8');await act('save');await act('yes');await dialog().getByText('已保存。').waitFor();
  const growth=(await req('context.source_save')).at(-1).arguments.fields;assert.deepEqual(growth.traits,['安静','好奇']);assert.deepEqual(growth.interests,{阅读:.5,写作:.8});
  await act('versions');await dialog().locator('[data-index="1"]').click();await act('restore');await act('yes');await p.waitForFunction(()=>document.querySelector('[name=reflection]')?.value==='最初反思');await act('close');
  const pick=p.locator('[data-preset-select]'),title=p.locator('[data-preset-name]');
  await ws('preset-new').click();await act('no');assert.equal(await guidance.inputValue(),'保留这份上下文草稿');
  await ws('preset-new').click();await act('yes');assert.equal(await guidance.inputValue(),'');assert.equal(await title.inputValue(),'');
  await title.fill('慢读');await guidance.fill('慢慢回答');await ws('preset-save').click();await p.locator('[data-edit-note]').getByText('已保存。').waitFor();
  assert.equal((await req('context.preset_apply')).length,0);
  const named=(await req('context.preset_save'))[0].arguments.id;
  await guidance.fill('暂存输入');await pick.selectOption('default');assert.equal(await guidance.inputValue(),'暂存输入');
  await ws('preset-load').click();await act('no');assert.equal(await title.inputValue(),'慢读');
  await ws('preset-load').click();await act('yes');await p.waitForFunction(()=>document.querySelector('[data-preset-name]').value==='默认');
  await pick.selectOption(named);assert.equal(await title.inputValue(),'默认');await ws('preset-load').click();
  await p.waitForFunction(()=>document.querySelector('[data-preset-name]').value==='慢读');
  await guidance.fill('这次应用');await p.evaluate(()=>{window.conflictPreset=true;});await ws('preset-save').click();await ws('reapply').click();
  assert.equal(await guidance.inputValue(),'这次应用');await ws('preset-save').click();await p.locator('[data-edit-note]').getByText('已保存。').waitFor();
  assert.equal((await req('context.preset_save')).at(-1).arguments.config.guidance.group,'另一窗口修改了群聊');
  await guidance.fill('还没有保存');await ws('preset-use').click();await p.locator('[data-apply-note]').getByText('已应用').waitFor();
  assert.equal(await guidance.inputValue(),'还没有保存');assert.equal((await req('context.preset_save')).length,3);
  assert.equal(await p.locator('#house-active-preset').textContent(),'慢读');
  await ws('traces').click();const records=dialog();await records.getByText('慢读方案的回复').waitFor();
  await ws('reply-more').click();await records.getByText('更早的慢读回复').waitFor();
  await p.evaluate(()=>{window.cleaned=['s'];dispatchEvent(new Event('focus'));});await records.getByText('慢读方案的回复',{exact:true}).waitFor({state:'hidden'});
  await records.locator('select').selectOption('default');await records.getByText('默认方案的回复').waitFor();await p.keyboard.press('Escape');
  assert.equal(await guidance.inputValue(),'还没有保存');await ws('preset-default').click();await p.getByText('已恢复默认',{exact:true}).waitFor();
  assert.equal(await p.locator('#house-active-preset').textContent(),'默认');assert.equal(await guidance.inputValue(),'还没有保存');
  await pick.selectOption(named);await ws('traces').click();await dialog().getByText('更早的慢读回复').waitFor();
  assert.equal(await dialog().getByText('慢读方案的回复',{exact:true}).count(),0);await act('close');
  assert.equal((await req('context.save')).length+(await req('context.publish')).length,0);
  await row('memory_records').click();await p.locator('#memory-items button').first().click();await p.locator('#memory-content').fill('改笔记');
  await p.locator('#memory-save').click();await act('no');assert.equal((await req('records.correct')).length,0);
  await p.locator('#memory-save').click();await act('yes');await waitCalls('records.correct',1);
  await p.locator('#memory-versions').click();await dialog().locator('[data-index="1"]').click();await dialog().getByText('旧笔记').waitFor();
  await act('restore');await act('yes');await p.waitForFunction(()=>document.querySelector('#memory-content').value==='旧笔记');
  await p.locator('#memory-items button').filter({hasText:'个人事实'}).click();await p.getByText('采用这条事实',{exact:true}).first().click();
  await p.getByText('使用这个旧值',{exact:true}).click();await p.locator('#fact-reason').fill('选择早期称呼');await p.locator('#fact-correct').click();await act('yes');
  await waitCalls('facts.restore_version',1);assert.equal((await req('facts.restore_version'))[0].arguments.version,'old-fact');await back();
  await row('relations').click();await p.locator('#relation-claims-items button').first().click();
  await p.getByText('修改声明正文',{exact:true}).click();await p.getByLabel('关系正文',{exact:true}).fill('朋友');
  await p.getByText('预览正文修改',{exact:true}).click();await p.getByText('确认提交',{exact:true}).waitFor();assert.equal((await req('relations.commit')).length,0);
  await p.getByText('取消这次预览',{exact:true}).click();await p.getByText('预览恢复更改前的版本',{exact:true}).click();
  await p.getByText('确认提交',{exact:true}).click();await waitCalls('relations.commit',1);await back();
  for(const layer of ['files','stickers']){
   await row(layer).click();
   const input=layer==='files'?p.locator('#file-edit-name-files'):p.locator('#sticker-detail-name');
   const save=layer==='files'?p.locator('#file-save-files'):p.locator('#sticker-detail-save');
   await input.fill('本地修改');await save.click();await act('no');assert.equal((await req(layer+'.configure')).length,0);
   await save.click();await act('yes');await waitCalls(layer+'.configure',1);
   const history=layer==='files'?p.locator('#file-items').getByText('历史版本'):p.locator('#sticker-versions');
   const replace=layer==='files'?p.locator('#file-items').getByText('替换原件'):p.locator('#sticker-replace');
   await history.click();await dialog().locator('[data-index="1"]').click();await dialog().getByText('旧原件').waitFor();await act('export');await act('restore');await act('yes');
   await p.locator('.ws-source-dialog').waitFor({state:'hidden'});await replace.click();await back();
  }
  assert.equal((await req('library-replace')).length,2);assert.equal((await req('library-export-version')).length,2);assert.equal((await req('library.restore')).length,2);
  await guidance.fill('不能跨身份');await row('summary').click();await dialog().locator('[data-index="0"]').click();
  await p.evaluate(()=>dispatchEvent(new CustomEvent('qiban-management-state',{detail:{ready:true,locked:false,roomSession:'second'}})));
  await p.locator('.ws-source-dialog').waitFor({state:'hidden'});await guidance.waitFor();assert.notEqual(await guidance.inputValue(),'不能跨身份');
  await guidance.fill('锁定时清除');await p.evaluate(()=>access(false));await p.getByText('解锁桌面管理后可查看。').waitFor();await p.evaluate(()=>access(true));
  await guidance.waitFor();assert.notEqual(await guidance.inputValue(),'锁定时清除');
  await guidance.fill('权限变化时清除');await row('files').click();await p.evaluate(()=>{window.allowed=['context.workspace'];});
  await back();assert.ok(await guidance.isDisabled());assert.notEqual(await guidance.inputValue(),'权限变化时清除');assert.deepEqual(errors,[]);
  console.log('通过：七类编辑入口、两个窗口宽度、源记录逐版恢复、事实与关系、替换和导出原件、独立保存及应用、真实回复随清理更新、默认方案、冲突、输入提示和管理会话隔离。');
 }finally{await browser.close();}
}
main().catch(err=>{console.error(err);process.exitCode=1;});
