(()=>{
 'use strict';
 // 表情包库：中间栏是导入、列表与即时反馈，右栏是选中表情的预览与编辑。
 // 名称、标签、启用状态只有一处编辑（右栏），只有一个“保存更改”，一次提交一个
 // stickers.configure 请求（沿用后台原有的单次写入与 expected 版本校验），
 // 因此不会再出现“名称已存、启用没存”的部分保存。
 const el=id=>document.getElementById(id),pending=new Map(),picks=new Map(),drafts=new Map(),saved=new Map(),notices=new Map();
 const pageSize=5;
 let allowed=false,busy=false,epoch=0,after='',items=[],selected='';
 // 一张卡片只有一个状态行：这条表情最近一次仍然成立的结果，否则就是“有未保存的更改”。
 const UNSAVED='未保存的更改：点击“保存更改”后生效。';
 function call(operation,args={},native=false){
  const id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(id);reject(new Error('操作未完成，请重新读取。'));},native?300000:15000);
   pending.set(id,{resolve,reject,timer});
   window.chrome.webview.postMessage(native?{action:'sticker-import',request_id:id,...args}:
    {action:'management',operation:'stickers.'+operation,arguments:args,request_id:id});
  });
 }
 function tags(text){return text.split(/[,，\n]/).map(v=>v.trim()).filter(Boolean);}
 // 与后台 stickers.describe 的存储规则对齐：标签去重保序，名称按原样，启用是布尔。
 function shape(value){
  return JSON.stringify({name:value.name,tags:[...new Set(value.tags||[])],enabled:!!value.enabled});
 }
 function item(identifier){return items.find(entry=>entry.id===identifier)||null;}
 // 草稿保存的是输入框里的原样文字，提交时才拆成标签，所以来回切换选中项不会改动已输入的内容。
 function liveDraft(){
  if(!item(selected))return null;
  return {name:el('sticker-detail-name').value,tagsText:el('sticker-detail-tags').value,enabled:el('sticker-detail-enabled').checked};
 }
 function dirty(){
  const current=item(selected),live=liveDraft();
  if(!current||!live)return false;
  return shape({name:live.name,tags:tags(live.tagsText),enabled:live.enabled})!==saved.get(current.id);
 }
 function showState(){
  const current=item(selected);
  if(!current){el('sticker-detail-feedback').textContent='';return;}
  el('sticker-detail-feedback').textContent=notices.get(current.id)||(dirty()?UNSAVED:'');
 }
 function controls(){
  for(const id of ['sticker-import','sticker-refresh','sticker-name','sticker-tags'])el(id).disabled=!allowed||busy;
  el('sticker-next').disabled=!allowed||busy||items.length!==pageSize;
  for(const [,button] of picks)button.disabled=!allowed||busy;
  const current=item(selected);
  for(const id of ['sticker-detail-name','sticker-detail-tags','sticker-detail-enabled'])el(id).disabled=!allowed||busy||!current;
  el('sticker-detail-save').disabled=!allowed||busy||!current||!dirty();
 }
 function feedback(message,identifier){
  el('sticker-notice').textContent=message;
  if(identifier)notices.set(identifier,message);
  showState();
  controls();
 }
 function paintDetail(){
  const current=item(selected),preview=el('sticker-detail-preview');
  preview.hidden=!current;
  if(current)preview.src=current.preview;
  else preview.removeAttribute('src');
  el('sticker-detail-title').textContent=current
   ?`${current.name} · ${current.enabled?'已启用':'已停用'} · ${current.frames>1?'动图（预览为首帧）':'静态图片'}`
   :'请选择一张表情。';
  const draft=current?(drafts.get(current.id)||{name:current.name,tagsText:current.tags.join('，'),enabled:current.enabled})
   :{name:'',tagsText:'',enabled:false};
  el('sticker-detail-name').value=draft.name;
  el('sticker-detail-tags').value=draft.tagsText;
  el('sticker-detail-enabled').checked=!!draft.enabled;
  showState();
  controls();
 }
 function paintSelection(){
  for(const [identifier,button] of picks)button.setAttribute('aria-pressed',String(identifier===selected));
 }
 function render(){
  picks.clear();el('sticker-items').replaceChildren();
  if(!allowed){paintDetail();return;}
  if(!items.length){el('sticker-items').textContent='表情库还没有素材。';paintDetail();return;}
  for(const entry of items){
   const button=document.createElement('button');
   button.type='button';button.className='sticker-pick';button.id='sticker-pick-'+entry.id;
   button.setAttribute('aria-label','选择表情：'+entry.name);
   const thumb=document.createElement('img');
   thumb.src=entry.preview;thumb.alt='';thumb.width=56;thumb.height=56;thumb.className='sticker-thumb';
   const text=document.createElement('span');
   text.textContent=`${entry.name} · ${entry.enabled?'已启用':'已停用'} · ${entry.frames>1?'动图（预览为首帧）':'静态图片'}`;
   button.append(thumb,text);
   button.onclick=()=>select(entry.id);
   el('sticker-items').append(button);picks.set(entry.id,button);
  }
  paintSelection();paintDetail();
 }
 function select(identifier){
  if(!allowed||busy)return;
  selected=item(identifier)?identifier:'';
  paintSelection();paintDetail();
 }
 async function perform(action,identifier,pendingMessage='正在读取表情……'){
  if(!allowed||busy)return;
  busy=true;const token=epoch;let completedMessage='';feedback(pendingMessage,identifier);
  try{
   const message=action?await action(token):null;
   if(!allowed||epoch!==token)return;
   if(message){completedMessage=message;feedback(message,identifier);}
   const result=await call('list',{after,limit:pageSize});
   if(!allowed||epoch!==token)return;
   items=result.items;
   // 列表是库自己的答案：它返回的就是这些表情的已保存状态；不在本页的草稿继续保留。
   for(const entry of items)saved.set(entry.id,shape(entry));
   if(!item(selected))selected=items.length?items[0].id:'';
   render();if(!message)feedback('表情列表已更新。',identifier);
  }catch(error){if(allowed&&epoch===token)feedback(completedMessage?completedMessage+' 列表刷新未完成：'+error.message:error.message,identifier);}
  finally{if(epoch===token){busy=false;controls();}}
 }
 // 输入只改这张表情的草稿和右栏状态：不读库、不提交、不弹窗；只有“保存更改”进这一条路径。
 const remember=()=>{
  const current=item(selected);
  if(!current)return;
  if(dirty())drafts.set(current.id,{...liveDraft(),revision:drafts.get(current.id)?.revision??current.revision});
  else drafts.delete(current.id);
  notices.delete(current.id);
  showState();controls();
 };
 for(const id of ['sticker-detail-name','sticker-detail-tags'])el(id).oninput=remember;
 el('sticker-detail-enabled').onchange=remember;
 el('sticker-detail-save').onclick=()=>{
  const current=item(selected);
  if(!current||!allowed||busy||!dirty())return;
  const args={identifier:current.id,name:el('sticker-detail-name').value,tags:tags(el('sticker-detail-tags').value),
   enabled:el('sticker-detail-enabled').checked,expected:drafts.get(current.id)?.revision??current.revision};
  remember();
  return perform(async token=>{
   // 一次请求带上名称、标签和启用状态：后台按一个事务写入，避免部分保存。
   await call('configure',args);
   if(!allowed||epoch!==token)return null;
   saved.set(current.id,shape({name:args.name,tags:args.tags,enabled:args.enabled}));
   drafts.delete(current.id);
   return args.enabled?'更改已保存，表情已启用。':'更改已保存，表情已停用。';
  },current.id,'正在保存更改……');
 };
 el('sticker-import').onclick=()=>perform(async()=>{
  const result=await call('',{name:el('sticker-name').value,tags:tags(el('sticker-tags').value)},true);
  after='';return result.duplicate?'相同图片已在库中，保留原来的名称、标签和启用状态。':'表情已导入，当前为停用；确认标签后可启用。';
 });
 el('sticker-refresh').onclick=()=>{after='';return perform();};
 el('sticker-next').onclick=()=>{after=items.at(-1)?.id||'';return perform();};
 window.addEventListener('qiban-management-state',({detail})=>{
  const next=detail.ready&&!detail.locked,changed=next!==allowed;allowed=next;
  el('sticker-panel').hidden=el('management-panel').hidden;
  el('sticker-detail-panel').hidden=el('management-panel').hidden;
  if(!changed)return;
  if(!allowed){
   epoch++;busy=false;items=[];after='';selected='';picks.clear();drafts.clear();saved.clear();notices.clear();
   for(const id of ['sticker-name','sticker-tags','sticker-detail-name','sticker-detail-tags'])el(id).value='';
   el('sticker-detail-enabled').checked=false;el('sticker-notice').textContent='';
   el('sticker-detail-feedback').textContent='';el('sticker-detail-preview').hidden=true;
   el('sticker-detail-title').textContent='请选择一张表情。';
  }
  render();if(allowed)void perform();
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management'){
   const job=pending.get(data.request_id);if(!job)return;clearTimeout(job.timer);pending.delete(data.request_id);
   if(data.response.ok)job.resolve(data.response.result);else job.reject(new Error(data.response.message||'表情操作未完成。'));
  }
  if(data.type==='management_error'){
   for(const job of pending.values()){clearTimeout(job.timer);job.reject(new Error(data.message));}pending.clear();
  }
 });
 render();
 window.QibanAutoRead?.register('sticker-refresh',{dirty:()=>dirty()||drafts.size>0});
})();
