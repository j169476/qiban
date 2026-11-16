(()=>{
 'use strict';
 const el=id=>document.getElementById(id),pending=new Map(),rows=new Map(),drafts=new Map(),notices=new Map(),saved=new Map();
 let allowed=false,busy=false,epoch=0,after='',items=[],targets=[],settings=null,noticeOwner=null;
 // One card, one button: the same “保存更改” sends the name, the tags, the scopes and the enabled
 // box. Until it is pressed, the card says so in its own status line.
 const UNSAVED='未保存的更改：点击“保存更改”后生效。';
 function call(operation,args={},native=false){
  const id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(id);reject(new Error('操作未完成，请重新读取。'));},native?300000:15000);
   pending.set(id,{resolve,reject,timer});
   window.chrome.webview.postMessage(native?{action:'file-import',request_id:id,...args}:
    {action:'management',operation:'files.'+operation,arguments:args,request_id:id});
  });
 }
 function tags(text){return text.split(/[,，\n]/).map(v=>v.trim()).filter(Boolean);}
 function scopeKey(scope){return JSON.stringify([scope.platform,scope.account_identity,scope.kind,scope.target_id]);}
 // The card is compared with the file library under the backend's own rules: file_library.describe()
 // strips tags and drops repeats while keeping their first order, scopes() sorts by session id, and
 // the name is stored exactly as it was sent. Tags are only deduplicated here for the comparison;
 // the request still sends them exactly as typed, so no saved tag order changes.
 function shape(value){
  return JSON.stringify({name:value.name,tags:[...new Set(value.tags||[])],scopes:(value.scopes||[]).map(scopeKey).sort(),enabled:!!value.enabled});
 }
 function controls(){
  for(const id of ['file-import','file-refresh','file-tags'])el(id).disabled=!allowed||busy;
  for(const id of ['file-count','file-total-mib','file-settings-save'])el(id).disabled=!allowed||busy||!settings;
  el('file-next').disabled=!allowed||busy||items.length!==5;
  for(const [id,row] of rows){
   for(const node of row.controls)node.disabled=!allowed||busy;
   showState(id);
  }
 }
 // A card has one status line, in its own role=status element: the result that still applies to what
 // the card is showing, and otherwise the fact that the edits on it are not saved yet. A result is
 // dropped by the edit that makes it untrue (see remember), so an earlier success cannot read as this
 // one. Nothing here moves focus and nothing is shown twice.
 function showState(identifier){
  const row=rows.get(identifier);if(!row)return;
  row.notice.textContent=notices.get(identifier)||(row.dirty()?UNSAVED:'');
 }
 // One page-level line and one line per card. The page-level line remembers which card its message
 // belongs to, so the edit that unmakes a card's result removes that result from the page line as
 // well; a message of another card or of the page itself is left alone.
 function feedback(message,identifier){
  el('file-notice').textContent=message;
  noticeOwner=identifier||null;
  if(identifier)notices.set(identifier,message);
  controls();
 }
 function render(){
  rows.clear();el('file-items').replaceChildren();
  if(!allowed){controls();return;}
  if(!items.length){el('file-items').textContent='文件库还没有素材。';controls();return;}
  for(const item of items){
   const card=document.createElement('article');card.className='sticker-card';
   const title=document.createElement('p');title.textContent=`${item.name} · ${item.enabled?'已启用':'已停用'} · ${item.bytes} 字节`;
   const draft=drafts.get(item.id)||{name:item.name,tags:item.tags.join('，'),scopes:item.scopes,enabled:item.enabled};
   const nameLabel=document.createElement('label');nameLabel.textContent='文件名称';nameLabel.htmlFor='file-edit-name-'+item.id;
   const name=document.createElement('input');name.id=nameLabel.htmlFor;name.value=draft.name;name.maxLength=180;name.setAttribute('aria-label','文件名称：'+item.name);
   const tagLabel=document.createElement('label');tagLabel.textContent='标签与适用语境（逗号分隔）';tagLabel.htmlFor='file-edit-tags-'+item.id;
   const label=document.createElement('input');label.id=tagLabel.htmlFor;label.value=draft.tags;label.placeholder='例如：使用说明，课件，活动资料';label.setAttribute('aria-label','文件标签：'+item.name);
   const scopeBox=document.createElement('fieldset'),legend=document.createElement('legend');legend.textContent='允许使用的会话';scopeBox.append(legend);
   const choices=[];
   const available=new Map(targets.map(t=>[scopeKey(t.scope),t.scope]));
   for(const scope of draft.scopes)if(!available.has(scopeKey(scope)))available.set(scopeKey(scope),scope);
   for(const scope of available.values()){
    const wrap=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.checked=draft.scopes.some(s=>scopeKey(s)===scopeKey(scope));
    const active=targets.some(t=>scopeKey(t.scope)===scopeKey(scope));
    const text=document.createElement('span');text.textContent=`QQ ${scope.account_identity} · ${scope.kind==='group'?'群':'好友'} ${scope.target_id}${active?'':'（会话授权已停用）'}`;
    wrap.append(input,text);scopeBox.append(wrap);choices.push({input,scope});
   }
   if(!available.size){const hint=document.createElement('p');hint.textContent='请先在QQ回复会话中选择并保存好友或群聊，再重新读取文件列表。';scopeBox.append(hint);}
   const selected=()=>choices.filter(c=>c.input.checked).map(c=>c.scope);
   const enabledLabel=document.createElement('label');enabledLabel.className='file-enabled';
   const enabled=document.createElement('input');enabled.type='checkbox';enabled.id='file-edit-enabled-'+item.id;enabled.checked=draft.enabled;
   const enabledText=document.createElement('span');enabledText.textContent='启用此文件（保存后生效）';enabledLabel.append(enabled,enabledText);
   const hint=document.createElement('p');hint.textContent='名称、标签、会话范围和启用状态统一保存；修改后点击“保存更改”生效。';
   // What the library currently has for this card. It moves to what a save sent (even when the list
   // refresh after that save fails) and to whatever a later refresh reports, so “未保存” keeps
   // describing this card's own edits rather than a stale success or a stale failure.
   if(!saved.has(item.id))saved.set(item.id,shape(item));
   const dirty=()=>shape({name:name.value,tags:tags(label.value),scopes:selected(),enabled:enabled.checked})!==saved.get(item.id);
   // Typing and ticking only change this card's draft and its own status line: nothing is read,
   // submitted, focused or popped up per keystroke, and the single save button stays the only way in.
   // The card's own last result is dropped here, including its copy on the page line, so a past
   // “已保存” never stands next to a card that is unsaved again. Nothing else is cleared.
   const remember=()=>{
    drafts.set(item.id,{name:name.value,tags:label.value,scopes:selected(),enabled:enabled.checked});
    notices.delete(item.id);
    if(noticeOwner===item.id){noticeOwner=null;el('file-notice').textContent='';}
    showState(item.id);
   };
   enabled.onchange=remember;name.oninput=remember;label.oninput=remember;
   for(const choice of choices)choice.input.onchange=remember;
   const save=document.createElement('button');save.textContent='保存更改';save.id='file-save-'+item.id;
   const actions=document.createElement('div');actions.className='actions';
   const notice=document.createElement('p');notice.className='file-feedback';notice.setAttribute('role','status');notice.id='file-feedback-'+item.id;
   actions.append(save,notice);
   save.onclick=()=>{
    const nextEnabled=enabled.checked;
    const args={identifier:item.id,name:name.value,tags:tags(label.value),enabled:nextEnabled,allowed_scopes:selected(),expected:item.revision};remember();
    return perform(async token=>{
     await call('configure',args);
     // The answer is only this card's new saved state while the run that sent it is still the
     // current one. A lock during the wait has already cleared every draft and saved state, and
     // this late answer must not write either of them back into the next session.
     if(!allowed||epoch!==token)return null;
     saved.set(item.id,shape({name:args.name,tags:args.tags,scopes:args.allowed_scopes,enabled:args.enabled}));
     drafts.delete(item.id);
     return nextEnabled?'更改已保存，文件已启用。':'更改已保存，文件已停用。';
    },item.id,'正在保存更改……');
   };
   card.append(title,nameLabel,name,tagLabel,label,scopeBox,enabledLabel,hint,actions);el('file-items').append(card);
   rows.set(item.id,{controls:[name,label,save,enabled,...choices.map(c=>c.input)],notice,dirty});
  }
  controls();
 }
 async function perform(action,identifier,pendingMessage='正在读取文件……'){
  if(!allowed||busy)return;busy=true;const token=epoch;let completedMessage='';feedback(pendingMessage,identifier);
  try{
   // The action gets the epoch it runs under: a request answered after a lock must not write into
   // the state that lock cleared, even though its own answer is a perfectly normal answer.
   const message=action?await action(token):null;
   if(!allowed||epoch!==token)return;
   if(message){completedMessage=message;feedback(message,identifier);}
   const result=await call('list',{after,limit:5});
   if(!allowed||epoch!==token)return;
   const scopeResult=await call('targets');
   if(!allowed||epoch!==token)return;
   items=result.items;targets=scopeResult.items;settings=result.settings||null;
   // The list is the library's own answer: whatever it returns is that card's saved state again.
   for(const item of items)saved.set(item.id,shape(item));
   if(settings){el('file-count').value=String(settings.count);el('file-total-mib').value=String(settings.total_mib);}
   render();if(!message)feedback('文件列表已更新。');
  }catch(error){if(allowed&&epoch===token)feedback(completedMessage?completedMessage+' 列表刷新未完成：'+error.message:error.message,identifier);}
  finally{if(epoch===token){busy=false;controls();}}
 }
 el('file-settings-save').onclick=()=>{
  if(!allowed||busy||!settings)return;
  const count=Number(el('file-count').value),total_mib=Number(el('file-total-mib').value);
  if(!Number.isInteger(count)||count<1||count>12||!Number.isInteger(total_mib)||total_mib<1||total_mib>120){feedback('请输入1至12份、1至120 MiB的整数上限。');return;}
  return perform(async()=>{await call('settings.configure',{count,total_mib,expected:settings.revision});return '文件发送上限已保存。';});
 };
 el('file-import').onclick=()=>perform(async()=>{
  const result=await call('',{tags:tags(el('file-tags').value)},true);
  after='';return result.duplicate?'相同文件已在库中，保留原来的名称、标签和授权设置。':'文件已导入，当前为停用；选择会话并勾选启用后，点击“保存更改”生效。';
 });
 el('file-refresh').onclick=()=>{after='';return perform();};
 el('file-next').onclick=()=>{after=items.at(-1)?.id||'';return perform();};
 window.addEventListener('qiban-management-state',({detail})=>{
  const next=detail.ready&&!detail.locked,changed=next!==allowed;allowed=next;
  el('file-panel').hidden=el('management-panel').hidden;
  if(!changed)return;
  if(!allowed){epoch++;busy=false;items=[];targets=[];settings=null;after='';rows.clear();drafts.clear();notices.clear();saved.clear();noticeOwner=null;for(const id of ['file-tags','file-count','file-total-mib'])el(id).value='';el('file-notice').textContent='';}
  render();if(allowed)void perform();
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management'){
   const job=pending.get(data.request_id);if(!job)return;clearTimeout(job.timer);pending.delete(data.request_id);
   if(data.response.ok)job.resolve(data.response.result);else job.reject(new Error(data.response.message||'文件操作未完成。'));
  }
  if(data.type==='management_error'){
   for(const job of pending.values()){clearTimeout(job.timer);job.reject(new Error(data.message));}pending.clear();
  }
 });
 render();
 window.QibanAutoRead?.register('file-refresh',{dirty:()=>drafts.size>0||!!settings&&(Number(el('file-count').value)!==settings.count||Number(el('file-total-mib').value)!==settings.total_mib)});
})();
