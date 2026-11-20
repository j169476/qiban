(() => {
 'use strict';
 const ui=window.QibanWorkspace,{escape:e}=ui;
 const names={summary:'会话摘要',growth:'私密成长',files:'文件',stickers:'表情'};
 const fields={content:'摘要正文',reflection:'反思',traits:'性格特点',speech_habits:'说话习惯',opinions:'看法',interests:'兴趣'};
 const notes={content:'记录这段聊天里值得记住的事情。',reflection:'写下龙娘从这段经历中学到的事。',traits:'每行写一种性格特点。',speech_habits:'每行写一种常用的表达方式。',opinions:'每行写一条她对事情的看法。'};
 const listFields=['traits','speech_habits','opinions'];
 const btn=(key,text,off=false)=>'<button type="button" class="secondary" data-do="'+key+'"'+(off?' disabled':'')+'>'+e(text)+'</button>';
 const prettyTime=at=>at&&Number.isFinite(Date.parse(at))?new Date(at).toLocaleString():'';
 function panel(title,parentSignal){
  const dialog=document.createElement('dialog'),controller=new AbortController(),signal=controller.signal;
  dialog.className='ws ws-source-dialog';dialog.setAttribute('aria-label',title);
  dialog.innerHTML='<header><h2>'+e(title)+'</h2>'+btn('close','关闭')+'</header><p role="status" class="ws-status"></p><fieldset class="source-body"></fieldset><section class="source-confirm"></section>';
  const body=dialog.querySelector('fieldset'),message=dialog.querySelector('[role=status]'),confirmation=dialog.querySelector('.source-confirm');
  let busy=false,guard=()=>false,answer=null;
  function finish(value){const callback=answer;answer=null;confirmation.replaceChildren();body.inert=false;callback?.(value);}
  function close(){controller.abort();finish(false);if(dialog.open)dialog.close();dialog.remove();}
  function ask(text,yes){
   if(answer||signal.aborted)return Promise.resolve(false);
   body.inert=true;confirmation.innerHTML=(body.hidden?'':'<p>'+e(text)+'</p>')+btn('yes',yes)+btn('no','取消');
   confirmation.querySelector('[data-do=no]').focus();
   return new Promise(resolve=>{answer=resolve;});
  }
  async function leave(){
   if(answer){finish(false);return;}
   if(!busy&&(!guard()||await ask('放弃本次修改？','放弃')))close();
  }
  dialog.addEventListener('cancel',ev=>{ev.preventDefault();void leave();});
  dialog.addEventListener('click',ev=>{
   const action=ev.target.closest('button')?.dataset.do;
   if(action==='yes'||action==='no')finish(action==='yes');
   else if(action==='close')void leave();
  });
  for(const event of ['qiban-workspace-access-change','qiban-nav-change','qiban-room-view'])window.addEventListener(event,close,{signal});
  if(parentSignal){
   parentSignal.addEventListener('abort',close,{once:true,signal});
   if(parentSignal.aborted){close();return null;}
  }
  function status(text){if(!signal.aborted)message.textContent=text;}
  async function run(fn,loading='加载中…'){
   if(busy||signal.aborted)return;
   busy=true;body.disabled=true;status(loading);
   try{await fn();}catch(error){if(!signal.aborted)status(error.message||'操作未完成。');}
   finally{busy=false;body.disabled=false;}
  }
  document.body.append(dialog);dialog.showModal();
  return {body,signal,close,ask,status,run,guard:fn=>{guard=fn;},call:(op,args)=>ui.call(op,args,signal)};
 }
 function lines(value){return String(value).split(/\r?\n/).map(s=>s.trim()).filter(Boolean);}
 function formValues(layer,body){
  const val=key=>body.querySelector('[name="'+key+'"]').value;
  if(layer==='summary')return {content:val('content')};
  const interests=Object.create(null);
  for(const row of body.querySelectorAll('[data-interest]')){
   const topic=row.querySelector('[name=topic]').value.trim(),raw=row.querySelector('[name=score]').value;
   if(!topic&&!raw)continue;
   if(!topic||!raw||!Number.isFinite(Number(raw))||Number(raw)<0||Number(raw)>1||Object.hasOwn(interests,topic))throw Error('每个兴趣需要不同的名称，权重在 0 到 1 之间。');
   interests[topic]=Number(raw);
  }
  return {reflection:val('reflection'),...Object.fromEntries(listFields.map(k=>[k,lines(val(k))])),interests};
 }
 function interest(topic='',score=''){
  const {field}=window.QibanPresets;
  return '<div data-interest class="ws-interest">'+field('兴趣名称','写下龙娘感兴趣的事。','<input name="topic" value="'+e(topic)+'">')+
   field('兴趣权重','从 0 到 1，越大表示越感兴趣。','<input name="score" type="number" min="0" max="1" step="any" value="'+e(score)+'">')+btn('remove-interest','移除')+'</div>';
 }
 function showFields(value,labels={}){
  return Object.entries(value||{}).map(([k,v])=>'<div><strong>'+e(labels[k]||fields[k]||({name:'名称',title:'标题',tags:'标签',bytes:'字节数',sha256:'SHA-256'})[k]||k)+'</strong><p>'+
   e(Array.isArray(v)?v.join('、'):v&&typeof v==='object'?Object.entries(v).map(([n,s])=>n+' · '+s).join('\n'):v)+'</p></div>').join('');
 }
 function history({title,signal,load,detail,restore,exportVersion}){
  const p=panel('历史版本 · '+title,signal);if(!p)return;
  let items=[],after=null,selected=null,shown=null;
  function paint(){
   p.body.innerHTML='<div class="ws-version-list">'+items.map((item,i)=>'<button type="button" class="secondary" data-index="'+i+'" aria-pressed="'+(selected===item)+'">'+
    e([prettyTime(item.at)||String(item.version||item.revision).slice(0,12),item.original?'初始版本':'',item.current?'当前版本':''].filter(Boolean).join(' · '))+'</button>').join('')+'</div>'+
    (after?btn('more','更多版本'):'')+(!items.length?'还没有历史版本。':'')+
    (shown?'<section class="ws-source-preview">'+(shown.image?'<img alt="表情预览" src="'+e(shown.image)+'">':'')+showFields(shown.fields,shown.labels)+'</section><div class="ws-actions">'+
    (exportVersion?btn('export','导出原件'):'')+(restore?btn('restore','恢复此版',selected.current):'')+'</div>':'');
  }
  async function more(){
   const page=await load(p,after);
   if(p.signal.aborted)return;
   items.push(...page.items.map(item=>({...item,original:!!page.original&&item.version===page.original})));after=page.next||page.after||null;paint();p.status('');
  }
  p.body.onclick=ev=>{
   const b=ev.target.closest('button');if(!b||b.disabled)return;
   if(b.dataset.index!==undefined){void p.run(async()=>{
    const item=items[Number(b.dataset.index)],view=await detail(p,item);
    if(p.signal.aborted)return;selected=item;shown=view;paint();p.status('');
   });return;}
   if(b.dataset.do==='more')void p.run(more);
   if(b.dataset.do==='export')void p.run(async()=>{await exportVersion(selected);p.status('');},'正在导出…');
   if(b.dataset.do==='restore')void (async()=>{
    if(!await p.ask('用该版本替换当前内容？','恢复'))return;
    await p.run(async()=>{await restore(p,selected,shown);if(!p.signal.aborted)p.close();},'正在恢复…');
   })();
  };
  void p.run(more);
 }
 function openEditor({layer,signal,has}){
  const p=panel(names[layer],signal);if(!p)return;
  let entries=[],after=null,current=null,modified=false,canSave=has('context.source_save'),conflict=false;
  p.guard(()=>modified);
  function showList(){
   current=null;modified=false;conflict=false;
   p.body.innerHTML='<div class="ws-source-list">'+entries.map((v,i)=>'<button type="button" class="secondary" data-index="'+i+'"><strong>'+e(v.title)+'</strong><small>'+e([prettyTime(v.at),v.preview].filter(Boolean).join(' · '))+'</small></button>').join('')+'</div>'+
    (entries.length?'':'还没有记录。')+(after?btn('more','更多记录'):'');
  }
  async function load(more){
   const value=await p.call('context.source_list',{layer,after:more?after:null});
   if(p.signal.aborted)return;
   entries=more?[...entries,...value.items]:value.items;after=value.after;showList();p.status('');
  }
  function edit(item){
   if(p.signal.aborted)return;current=item;modified=false;conflict=false;
   const keys=layer==='summary'?['content']:['reflection',...listFields];
   p.body.innerHTML='<h3>'+e(item.title)+'</h3><fieldset>'+keys.map(k=>window.QibanPresets.field(fields[k],notes[k],
    '<textarea name="'+k+'" rows="3"'+(canSave?'':' disabled')+'>'+e(Array.isArray(item.fields[k])?item.fields[k].join('\n'):item.fields[k])+'</textarea>')).join('')+
    (layer==='growth'?'<p>兴趣与权重（0–1）</p><div data-interests>'+Object.entries(item.fields.interests||{}).map(([t,v])=>interest(t,v)).join('')+'</div>'+btn('add-interest','添加兴趣'):'')+
    '</fieldset><div data-latest></div><div class="ws-actions">'+(canSave?btn('save','保存',true):'')+
    (has('context.source_versions')&&has('context.source_version')?btn('versions','历史版本'):'')+btn('list','返回列表')+'</div>';
   if(!canSave)for(const control of p.body.querySelectorAll('[data-interests] input,[data-do=add-interest],[data-do=remove-interest]'))control.disabled=true;
   p.status('');
  }
  function changed(){
   modified=true;if(!conflict)p.status('');
   const b=p.body.querySelector('[data-do=save]');if(b)b.disabled=!canSave||conflict;
  }
  async function save(f,expected){
   const saved=await p.call('context.source_save',{layer,id:current.id,expected,fields:f});
   if(p.signal.aborted)return;
   const entry=entries.find(v=>v.id===saved.id);
   if(entry){entry.title=saved.title;entry.preview=String(saved.fields.content??saved.fields.reflection??'').slice(0,120);}
   edit(saved);p.status('已保存。');
  }
  async function showConflict(){
   conflict=true;changed();p.status('这条资料已在别处修改，你的输入已保留。');
   const box=p.body.querySelector('[data-latest]');
   box.innerHTML=btn('retry-latest','重新读取最新内容');
   const latest=await p.call('context.source_detail',{layer,id:current.id});
   if(p.signal.aborted)return;
   box.innerHTML='<h4>最新内容</h4>'+showFields(latest.fields)+btn('keep','保留我的输入')+btn('use-latest','使用最新内容');
   box.onclick=ev=>{
    const choice=ev.target.closest('button')?.dataset.do;
    if(choice==='use-latest'){edit(latest);return;}
    if(choice==='keep'){current=latest;conflict=false;box.replaceChildren();changed();p.status('');}
   };
  }
  p.body.oninput=changed;
  p.body.onclick=ev=>{
   const b=ev.target.closest('button');if(!b||b.disabled)return;
   const a=b.dataset.do;
   if(b.dataset.index!==undefined){void p.run(async()=>edit(await p.call('context.source_detail',{layer,id:entries[Number(b.dataset.index)].id})));return;}
   if(a==='more')void p.run(()=>load(true));
   if(a==='retry-latest')void p.run(showConflict);
   if(a==='list')void (async()=>{if(!modified||await p.ask('放弃本次修改？','放弃')){showList();p.status('');}})();
   if(a==='add-interest'){p.body.querySelector('[data-interests]').insertAdjacentHTML('beforeend',interest());p.body.querySelector('[data-interest]:last-child input').focus();changed();}
   if(a==='remove-interest'){b.closest('[data-interest]').remove();changed();}
   if(a==='save')void (async()=>{
    let f;try{f=formValues(layer,p.body);}catch(error){p.status(error.message);return;}
    if(!await p.ask('保存修改？','保存'))return;
    await p.run(async()=>{try{await save(f,current.version);}catch(error){if(error.code==='edit_conflict'){await showConflict();return;}throw error;}},'正在保存…');
   })();
   if(a==='versions')history({title:current.title,signal:p.signal,load:(h,after)=>h.call('context.source_versions',{layer,id:current.id,after}),
    detail:async(h,item)=>({fields:(await h.call('context.source_version',{layer,id:current.id,version:item.version})).fields}),
    restore:canSave?async(h,item,view)=>{
     try{await save(view.fields,current.version);}
     catch(err){if(err.code==='edit_conflict'){h.close();await showConflict();}else throw err;}
    }:null});
  };
  void p.run(()=>load(false));
 }
 function recordHistory({kind,identifier,expected,title,update}){
  history({title,load:(p,after)=>p.call('records.versions',{kind,identifier,after,limit:20}),
   detail:async(p,item)=>{
    const {data}=await p.call('records.version',{kind,identifier,version:item.version});
    return {labels:{content:'正文'},fields:Object.fromEntries(['title','content','tags'].filter(k=>k in data).map(k=>[k,data[k]]))};
   },restore:async(p,item)=>{const value=await p.call('records.restore_version',{kind,identifier,version:item.version,expected});if(!p.signal.aborted)update(value);}});
 }
 function libraryHistory({layer,item,update,native}){
  history({title:item.name,load:(p,after)=>p.call('library.versions',{layer,identifier:item.id,after}),
   detail:async(p,v)=>({fields:{name:v.name,tags:v.tags,bytes:v.bytes,sha256:v.sha256},image:/^data:image\/(?:png|jpeg|webp|gif);base64,/i.test(v.preview)?v.preview:null}),
   exportVersion:v=>native('library-export-version',{layer,identifier:item.id,revision:v.revision}),
   restore:async(p,v)=>{await p.call('library.restore',{layer,identifier:item.id,revision:v.revision,expected:item.revision});if(!p.signal.aborted)await update();}});
 }
 // Existing source forms keep their original validation and write paths after confirmation.
 let approved=null;
 document.addEventListener('click',ev=>{
  const b=ev.target.closest('button');
  if(!b||b.disabled||b===approved||!(/^(memory-save|sticker-detail-save|fact-correct|fact-confirm)$/.test(b.id)||b.id.startsWith('file-save-')))return;
  ev.preventDefault();ev.stopImmediatePropagation();
  const p=panel('保存修改？');if(!p)return;
  p.body.hidden=true;
  void p.ask('保存修改？','保存').then(ok=>{
   p.close();if(!ok||!b.isConnected||b.disabled)return;
   approved=b;try{b.click();}finally{approved=null;}
  });
 },true);
 let back=null;
 for(const event of ['qiban-workspace-access-change','qiban-nav-change','qiban-tab-change','qiban-room-view']){
  window.addEventListener(event,()=>{back?.remove();back=null;});
 }
 function openPanel(page,tab,id,source){
  const panel=document.getElementById(id);
  document.querySelector('[data-nav-target="'+page+'"]').click();
  document.querySelector('nav[data-nav-page="'+page+'"] [data-tab-target="'+tab+'"]').click();
  back=document.createElement('div');const link=document.createElement('button');link.type='button';link.className='secondary ws-source-return';link.textContent='返回上下文';
  link.onclick=()=>document.querySelector('[data-nav-target=context]').click();back.append(link);
  if(source==='catalog'){
   const note=document.createElement('p');note.className='note';note.textContent='在这里修改目录引用的记忆、知识或日记。';back.append(note);
  }
  panel.prepend(back);link.focus({preventScroll:true});panel.scrollIntoView({block:'start'});
 }
 window.QibanSources={openEditor,openPanel,recordHistory,libraryHistory,panel};
})();
