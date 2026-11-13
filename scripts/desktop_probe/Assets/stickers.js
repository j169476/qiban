(()=>{
 'use strict';
 const el=id=>document.getElementById(id),pending=new Map(),rows=new Map(),drafts=new Map(),notices=new Map();
 let allowed=false,busy=false,epoch=0,after='',items=[];
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
 function controls(){
  for(const id of ['sticker-import','sticker-refresh','sticker-name','sticker-tags'])el(id).disabled=!allowed||busy;
  el('sticker-next').disabled=!allowed||busy||items.length!==5;
  for(const [id,row] of rows){
   for(const node of row.controls)node.disabled=!allowed||busy;
   row.notice.textContent=notices.get(id)||'';
  }
 }
 function feedback(message,identifier){
  el('sticker-notice').textContent=message;
  if(identifier)notices.set(identifier,message);
  controls();
 }
 function render(){
  rows.clear();el('sticker-items').replaceChildren();
  if(!allowed){controls();return;}
  if(!items.length){el('sticker-items').textContent='表情库还没有素材。';controls();return;}
  for(const item of items){
   const card=document.createElement('article'),preview=document.createElement('img');card.className='sticker-card';
   preview.src=item.preview;preview.alt=item.name;preview.width=96;preview.height=96;preview.style.objectFit='contain';
   const title=document.createElement('p');title.textContent=`${item.name} · ${item.enabled?'已启用':'已停用'} · ${item.frames>1?'动图（预览为首帧）':'静态图片'}`;
   const draft=drafts.get(item.id)||{name:item.name,tags:item.tags.join('，')};
   const nameLabel=document.createElement('label');nameLabel.textContent='表情名称';nameLabel.htmlFor='sticker-edit-name-'+item.id;
   const name=document.createElement('input');name.id=nameLabel.htmlFor;name.value=draft.name;name.maxLength=100;name.setAttribute('aria-label','表情名称：'+item.name);
   const tagLabel=document.createElement('label');tagLabel.textContent='标签与适用语境（逗号分隔）';tagLabel.htmlFor='sticker-edit-tags-'+item.id;
   const label=document.createElement('input');label.id=tagLabel.htmlFor;label.value=draft.tags;label.placeholder='例如：开心，问候，赞同';label.setAttribute('aria-label','表情标签：'+item.name);
   const remember=()=>drafts.set(item.id,{name:name.value,tags:label.value});name.oninput=remember;label.oninput=remember;
   const save=document.createElement('button');save.textContent='保存名称和标签';save.id='sticker-save-'+item.id;
   const toggle=document.createElement('button');toggle.textContent=item.enabled?'停用':'启用';toggle.className='secondary';toggle.id='sticker-toggle-'+item.id;
   const actions=document.createElement('div');actions.className='actions';actions.append(save,toggle);
   const notice=document.createElement('p');notice.className='sticker-feedback';notice.setAttribute('role','status');notice.id='sticker-feedback-'+item.id;
   const change=(enabled,toggling)=>{
    const args={identifier:item.id,name:name.value,tags:tags(label.value),enabled,expected:item.revision};remember();
    return perform(async()=>{await call('configure',args);drafts.delete(item.id);return toggling?(enabled?'表情已启用。':'表情已停用。'):'名称和标签已保存。';},item.id,toggling?(enabled?'正在启用表情……':'正在停用表情……'):'正在保存名称和标签……');
   };
   save.onclick=()=>change(item.enabled,false);toggle.onclick=()=>change(!item.enabled,true);
   card.append(preview,title,nameLabel,name,tagLabel,label,actions,notice);el('sticker-items').append(card);
   rows.set(item.id,{controls:[name,label,save,toggle],notice});
  }
  controls();
 }
 async function perform(action,identifier,pendingMessage='正在读取表情……'){
  if(!allowed||busy)return;busy=true;const token=epoch;let completedMessage='';feedback(pendingMessage,identifier);
  try{
   const message=action?await action():null;
   if(!allowed||epoch!==token)return;
   if(message){completedMessage=message;feedback(message,identifier);}
   const result=await call('list',{after,limit:5});
   if(!allowed||epoch!==token)return;
   items=result.items;render();if(!message)feedback('表情列表已更新。');
  }catch(error){if(allowed&&epoch===token)feedback(completedMessage?completedMessage+' 列表刷新未完成：'+error.message:error.message,identifier);}
  finally{if(epoch===token){busy=false;controls();}}
 }
 el('sticker-import').onclick=()=>perform(async()=>{
  const result=await call('',{name:el('sticker-name').value,tags:tags(el('sticker-tags').value)},true);
  after='';return result.duplicate?'相同图片已在库中，保留原来的名称、标签和启用状态。':'表情已导入，当前为停用；确认标签后可启用。';
 });
 el('sticker-refresh').onclick=()=>{after='';return perform();};
 el('sticker-next').onclick=()=>{after=items.at(-1)?.id||'';return perform();};
 window.addEventListener('qiban-management-state',({detail})=>{
  const next=detail.ready&&!detail.locked,changed=next!==allowed;allowed=next;
  el('sticker-panel').hidden=el('management-panel').hidden;
  if(!changed)return;
  if(!allowed){epoch++;busy=false;items=[];after='';rows.clear();drafts.clear();notices.clear();for(const id of ['sticker-name','sticker-tags'])el(id).value='';el('sticker-notice').textContent='';}
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
})();
