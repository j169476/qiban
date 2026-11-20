(()=>{
 'use strict';
 const el=id=>document.getElementById('reply-quota-'+id),pending=new Map();
 const ORDER=['private_daily_replies','group_session_replies','group_session_decisions','group_session_minutes'];
 let allowed=false,busy=false,epoch=0,stale=false;
 let summary=null,bounds={},fields={},defaults={},day='',timezone='';
 const open=new Map(),drafts=new Map(),notes=new Map();
 const keyOf=account=>account.key||account.platform+':'+account.account;
 const noteKey=(kind,id)=>kind==='panel'?'panel':kind+'|'+id;
 const draftKey=(kind,id,field)=>kind+'|'+id+'|'+field;
 const platformName=account=>account.platform==='qq'?'QQ':'微信';
 function accountTitle(account){
  const label=account.label&&account.label!==account.account?'（'+account.label+'）':'';
  return platformName(account)+' · '+account.account+label+(account.selected?' · 当前使用':' · 已保存');
 }
 function stageText(window){
  if(!window||!window.id)return '群参与阶段：尚无阶段，本阶段暂无消耗。';
  const until=window.expires?new Date(window.expires*1000).toLocaleString('zh-CN',{hour12:false}):'';
  return '群参与阶段：'+window.note+'。本阶段已用 '+window.used+'/'+window.reply_limit+' 条，决策 '+
   window.decisions+'/'+window.decision_limit+' 次'+(until?'，阶段定时至 '+until:'')+'。';
 }
 function usageText(row){
  if(row.usage_error)return '私聊今日：'+row.usage_error;
  const used=row.usage||{confirmed:0,reserved:0,uncertain:0};
  return '私聊今日（'+day+'，'+timezone+'）：已确认 '+used.confirmed+' 条，预留 '+used.reserved+
   ' 条，未确认 '+used.uncertain+' 条；当前上限 '+row.limit+' 条。';
 }
 function describe(row){
  const source=row.inherited?'额度：继承账号默认':'额度：单独设置';
  return ['授权状态：'+row.status,source,row.kind==='group'?stageText(row.window):usageText(row)].join('；');
 }
 function timeText(seconds){
  return seconds?new Date(seconds*1000).toLocaleString('zh-CN',{hour12:false}):'';
 }
 // The exclusion state is shown separately from the authorization text, but the
 // authorization line already says "已排除" first so it cannot read as allowed.
 function exclusionText(row){
  if(row.excluded)return '排除状态：已排除（停用于 '+timeText(row.excluded_since)+'）；该会话不会回复，'+
   '其他会话、账号与平台不受影响；解除后只回应解除之后的新消息，排除期间的消息不会补回复。';
  if(row.reply_from)return '排除状态：已于 '+timeText(row.reply_from)+' 解除；只回应这之后的新消息，'+
   '解除排除不等于已恢复回复。';
  return '排除状态：未排除。';
 }
 function rule(){
  return '私聊按 '+timezone+' 时间每天零点重新计数。群聊每次被 @ 或引用时开始新一轮。';
 }
 function feedback(kind,id){
  const node=document.createElement('span');
  node.className='reply-quota-feedback';node.setAttribute('role','status');
  node.textContent=notes.get(noteKey(kind,id))||'';
  return node;
 }
 function say(kind,id,text){notes.set(noteKey(kind,id),text);}
 function fieldsFor(keys,values,kind,id){
  const wrap=document.createElement('div');wrap.className='reply-quota-fields';
  const inputs={};
  const locked=!allowed||busy||stale;
  for(const key of keys){
   const label=document.createElement('label');
   label.textContent=fields[key]||key;
   const input=document.createElement('input');
   input.type='number';input.step='1';
   input.min=String(bounds[key][0]);input.max=String(bounds[key][1]);
   const saved=drafts.get(draftKey(kind,id,key));
   input.value=saved!==undefined?saved:String(values[key]);
   // Editing stops while a request is in flight and after a version conflict, so a
   // late forget() cannot silently drop text typed in the meantime.
   input.disabled=locked;
   input.oninput=()=>{drafts.set(draftKey(kind,id,key),input.value);};
   label.append(input);wrap.append(label);inputs[key]=input;
  }
  return {wrap,inputs};
 }
 // Strict integers: no empty value, sign, decimal or separator is truncated or padded.
 function numbers(inputs,keys,say){
  const changes={};
  for(const key of keys){
   const raw=inputs[key].value;
   if(typeof raw!=='string'||!/^(0|[1-9][0-9]{0,8})$/.test(raw)){
    say((fields[key]||key)+'必须填写 0 或正整数，不能留空。');return null;
   }
   const value=Number(raw);
   const range=bounds[key];
   if(!range||value<range[0]||value>range[1]){
    say((fields[key]||key)+'必须在 '+(range?range[0]+' 至 '+range[1]:'允许范围')+' 之间。');return null;
   }
   changes[key]=value;
  }
  return changes;
 }
 function forget(kind,id,keys){
  notes.delete(noteKey(kind,id));
  for(const key of keys)drafts.delete(draftKey(kind,id,key));
 }
 function scopeBlock(account,row){
  const box=document.createElement('article');box.className='reply-quota-scope';
  const head=document.createElement('h4');head.textContent=row.label;box.append(head);
  const state=document.createElement('p');state.className='note';state.textContent=describe(row);box.append(state);
  const excluded=document.createElement('p');excluded.className='note';
  excluded.textContent=exclusionText(row);box.append(excluded);
  const editor=fieldsFor(row.fields,row.settings,'scope',row.scope_id);box.append(editor.wrap);
  const actions=document.createElement('div');actions.className='actions';
  const save=document.createElement('button');save.textContent='保存更改';
  save.disabled=!allowed||busy||stale;save.onclick=()=>saveScope(account,row,editor.inputs);
  const reset=document.createElement('button');reset.className='secondary';
  reset.textContent='恢复账号默认';
  // A conversation that already inherits has nothing to restore.
  reset.disabled=!allowed||busy||stale||row.inherited;
  reset.onclick=()=>resetScope(account,row);
  // Stopping or lifting one conversation never pauses the account and never saves
  // the quota fields: an unsaved draft above stays exactly as typed.
  const pause=document.createElement('button');pause.className='secondary';
  pause.textContent='停用此会话';
  pause.disabled=!allowed||busy||stale||row.excluded;
  pause.onclick=()=>setExclusion(account,row,true);
  const resume=document.createElement('button');resume.className='secondary';
  resume.textContent='解除排除';
  resume.disabled=!allowed||busy||stale||!row.excluded;
  resume.onclick=()=>setExclusion(account,row,false);  actions.append(save,reset,pause,resume);
  box.append(actions);box.append(feedback('scope',row.scope_id));
  return box;
 }
 function scopeList(account,view){
  const box=document.createElement('div');box.className='reply-quota-scopes';
  const state=document.createElement('p');state.className='note';
  const items=view.page.items;
  state.textContent='单会话额度：已登记会话共 '+view.page.total+' 个'+(items.length?
   ('，本页显示第 '+(view.page.offset+1)+' 至 '+(view.page.offset+items.length)+' 个'):'')+
   '。修改一个会话只会影响该会话；回到账号默认请点“恢复账号默认”。'+
   '“停用此会话”只停该会话，“解除排除”只取消该会话的排除，都不停用账号或平台。';
  box.append(state);
  if(!items.length){
   const empty=document.createElement('p');empty.className='note';
   empty.textContent='这个账号还没有已登记的会话。账号默认额度仍可修改。';box.append(empty);
  }
  for(const row of items)box.append(scopeBlock(account,row));
  const pager=document.createElement('div');pager.className='actions';
  const previous=document.createElement('button');previous.className='secondary';previous.textContent='上一页';
  previous.disabled=!allowed||busy||view.page.previous===null;
  previous.onclick=()=>loadPage(account,view.page.previous);
  const next=document.createElement('button');next.className='secondary';next.textContent='下一页';
  next.disabled=!allowed||busy||view.page.next===null;
  next.onclick=()=>loadPage(account,view.page.next);
  pager.append(previous,next);box.append(pager);
  return box;
 }
 function countText(account){
  const excluded=Number.isFinite(account.excluded)?account.excluded:0;
  const parts=['已登记会话 '+account.registered+' 个','当前有效 '+account.eligible+' 个',
   '手动勾选 '+account.manual+' 个'];
  if(excluded>0)parts.push('已排除 '+excluded+' 个');
  const allowAll=[account.allow_all&&account.allow_all.private?'允许所有好友已开启':'',
   account.allow_all&&account.allow_all.group?'允许所有群已开启':''].filter(Boolean);
  return parts.join('，')+'。'+(allowAll.length?allowAll.join('、')+'；运行时按关系核验后生效，不是撤销。':'');
 }
 function accountBlock(account){
  const box=document.createElement('section');box.className='reply-quota-account';
  const head=document.createElement('h3');head.textContent=accountTitle(account);box.append(head);
  const state=document.createElement('p');state.className='note';
  state.textContent=platformName(account)+'平台当前'+(account.platform_running?'正在运行，保存后的额度在下一次处理时生效。'
   :'未运行；额度可以离线保存，下次运行按已保存的额度执行。')+countText(account);
  box.append(state);
  const label=document.createElement('p');label.className='note';
  label.textContent='账号默认额度（影响所有未单独设置的会话）：';
  box.append(label);
  const id=keyOf(account);
  const editor=fieldsFor(ORDER,account.settings||defaults,'account',id);
  box.append(editor.wrap);
  const actions=document.createElement('div');actions.className='actions';
  const save=document.createElement('button');save.textContent='保存更改';
  save.disabled=!allowed||busy||stale;save.onclick=()=>saveAccount(account,editor.inputs);
  actions.append(save);
  const toggle=document.createElement('button');toggle.className='secondary';
  const view=open.get(id);
  toggle.textContent=view?'收起已登记会话':'查看已登记会话';
  toggle.disabled=!allowed||busy;toggle.onclick=()=>toggleScopes(account);
  actions.append(toggle);
  box.append(actions);box.append(feedback('account',id));
  if(view)box.append(scopeList(account,view));
  return box;
 }
 function render(){
  el('refresh').disabled=!allowed||busy;
  el('rule').textContent=summary?rule():'';
  el('status').textContent=notes.get('panel')||'';
  el('accounts').replaceChildren();
  if(!summary)return;
  if(!summary.accounts.length){
   const empty=document.createElement('p');empty.className='note';
   empty.textContent=summary.offset?'没有更多账号了。':'本机还没有可编辑额度的账号。请先在 QQ 或微信中完成一次账号核验并保存回复会话；'+
    '额度只对已保存并曾核验的账号开放，未核验的账号不能修改。';
   el('accounts').append(empty);
  }
  for(const account of summary.accounts)el('accounts').append(accountBlock(account));
  if(summary.next){
   const more=document.createElement('div');more.className='actions';
   const button=document.createElement('button');button.className='secondary';
   button.textContent='继续读取更多账号（已显示 '+summary.accounts.length+'/'+summary.total+' 个）';
   button.disabled=!allowed||busy||stale;button.onclick=()=>refresh(summary.next,true);
   more.append(button);el('accounts').append(more);
  }
 }
 function reset(){
  epoch++;busy=false;stale=false;summary=null;open.clear();drafts.clear();notes.clear();render();
 }
 function call(operation,args){
  const request_id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(request_id);reject(Error('操作超时，请重新读取回复额度。'));},15000);
   pending.set(request_id,{resolve,reject,timer});
   window.chrome.webview.postMessage({action:'management',operation,arguments:args,request_id});
  });
 }
 async function work(job,kind,id){
  if(!allowed||busy)return;
  const current=epoch;
  busy=true;render();
  try{await job(()=>allowed&&epoch===current);}
  catch(error){
   if(epoch!==current)return;
   // A stale page must never silently overwrite a newer value.
   if(error.code==='edit_conflict'){stale=true;say(kind,id,'额度已在别处改变，请重新读取后再保存。');}
   else if(error.code==='desktop_locked'){allowed=false;reset();return;}
   else say(kind,id,error.message||'操作未完成，请重新读取回复额度。');
  }
  finally{if(epoch===current){busy=false;render();}}
 }
 async function refresh(after,append){
  return work(async live=>{
   notes.delete('panel');
   const value=await call('reply_quota.summary',{after:after||''});
   if(!live())return;
   const accounts=value.accounts||[];
   summary={accounts:append&&summary?summary.accounts.concat(accounts):accounts,
    next:value.next,total:value.total,offset:value.offset};
   bounds=value.bounds||{};fields=value.fields||{};
   defaults=value.defaults||{};day=value.day||'';timezone=value.timezone||'';
   // A full re-read clears every stale version and draft; appending a later page
   // must keep the edits already typed on the accounts shown before it.
   if(!append){
    stale=false;open.clear();drafts.clear();
    for(const key of Array.from(notes.keys()))if(key!=='panel')notes.delete(key);
   }
   notes.set('panel','已读取 '+summary.accounts.length+'/'+summary.total+' 个账号的回复额度。');
  },'panel','panel');
 }
 async function loadPage(account,cursor){
  const id=keyOf(account);
  // Paging failures belong to the account block that asked for them.
  return work(async live=>{
   notes.delete(noteKey('account',id));
   const page=await call('reply_quota.scopes',{platform:account.platform,account:account.account,after:cursor});
   if(!live())return;
   open.set(id,{page});
  },'account',id);
 }
 function toggleScopes(account){
  const id=keyOf(account);
  if(open.has(id)){open.delete(id);render();return;}
  void loadPage(account,'');
 }
 async function saveAccount(account,inputs){
  const id=keyOf(account);
  return work(async live=>{
   notes.delete(noteKey('account',id));
   const changes=numbers(inputs,ORDER,text=>say('account',id,text));
   if(!changes)return;
   const saved=await call('reply_quota.account_save',
    {platform:account.platform,account:account.account,changes,expected:account.version});
   if(!live())return;
   for(const key of ORDER){account.settings[key]=saved.settings[key];}
   account.version=saved.version;
   forget('account',id,ORDER);
   say('account',id,saved.changed?'账号默认额度已保存；只影响继承账号默认的会话，单独设置的会话不受影响。'
    :'这些数值与已保存的账号默认额度相同，没有产生新的版本。');
   // Inheriting conversations change version with the default, so their cached
   // page is dropped instead of being edited against a stale version.
   open.delete(id);
  },'account',id);
 }
 async function saveScope(account,row,inputs){
  const id=keyOf(account);
  return work(async live=>{
   notes.delete(noteKey('scope',row.scope_id));
   const changes=numbers(inputs,row.fields,text=>say('scope',row.scope_id,text));
   if(!changes)return;
   const saved=await call('reply_quota.scope_save',
    {scope_id:row.scope_id,account:row.account,expected:row.version,changes});
   if(!live())return;
   forget('scope',row.scope_id,row.fields);
   replaceRow(id,saved);
   say('scope',row.scope_id,saved.changed?'该会话的额度已单独保存。'
    :'这些数值与已保存的额度相同，没有产生新的版本。');
  },'scope',row.scope_id);
 }
 async function resetScope(account,row){
  const id=keyOf(account);
  return work(async live=>{
   notes.delete(noteKey('scope',row.scope_id));
   const saved=await call('reply_quota.scope_reset',
    {scope_id:row.scope_id,account:row.account,expected:row.version});
   if(!live())return;
   forget('scope',row.scope_id,row.fields);
   replaceRow(id,saved);
   say('scope',row.scope_id,saved.changed?'已恢复为继承账号默认额度。':'该会话本来就使用账号默认额度。');
  },'scope',row.scope_id);
 }
 function replaceRow(accountId,saved){
  const view=open.get(accountId);
  if(!view)return;
  view.page.items=view.page.items.map(row=>row.scope_id===saved.scope_id?saved:row);
 }
 async function setExclusion(account,row,excluded){
  const id=keyOf(account);
  // A locked, busy or conflicted page never writes: it must be re-read first.
  if(!allowed||busy||stale||row.excluded===excluded)return;
  return work(async live=>{
   notes.delete(noteKey('scope',row.scope_id));
   // This is not a quota save: unsaved quota drafts stay, and the version checked is
   // the exclusion revision, so a stale page cannot lift a newer exclusion.
   const saved=await call(excluded?'reply_exclusion.exclude':'reply_exclusion.restore',
    {scope_id:row.scope_id,account:row.account,expected:row.exclusion_version});
   if(!live())return;
   replaceRow(id,saved);
   // Trustworthy counts for this account only; other accounts, drafts and expanded
   // pages are left untouched.
   if(saved.counts){
    for(const key of ['registered','manual','eligible','excluded']){
     if(typeof saved.counts[key]==='number')account[key]=saved.counts[key];
    }
   }
   say('scope',row.scope_id,saved.message||(excluded?'已停用此会话。':'已解除排除。'));
  },'scope',row.scope_id);
 }
 el('refresh').onclick=()=>(allowed&&!busy)?refresh(''):undefined;
 window.addEventListener('qiban-management-state',({detail})=>{
  const now=detail.ready&&!detail.locked;
  document.getElementById('reply-quota-panel').hidden=document.getElementById('management-panel').hidden;
  if(now===allowed)return;
  allowed=now;reset();
  if(allowed)void refresh('');
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  const job=data&&data.request_id?pending.get(data.request_id):null;
  if(data&&data.type==='management'&&job){
   clearTimeout(job.timer);pending.delete(data.request_id);
   if(data.response.ok)job.resolve(data.response.result);
   else{
    const error=Error(data.response.message);
    error.code=data.response.error||'';
    job.reject(error);
   }
  }else if(data&&data.type==='management_error'){
   for(const job of pending.values()){
    clearTimeout(job.timer);
    const error=Error(data.message);
    error.code=data.code||'';
    job.reject(error);
   }
   pending.clear();
  }
 });
 render();
 window.QibanAutoRead?.register('reply-quota-refresh',{dirty:()=>drafts.size>0});
})();
