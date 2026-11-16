(()=>{
 'use strict';
 const el = id => document.getElementById('audience-'+id);
 const pending = new Map();
 let allowed=false, busy=false, epoch=0, state=null, preview=null;
 let profiles=null;
 const accountDetails=new Map();
 function accountLabel(login){
  const item=accountDetails.get(login);
  return item?`${item.nickname||'未提供昵称'} · ${item.wechat_id?'微信号：'+item.wechat_id:login}`:login;
 }
 function paintIdentity(login){
  const item=accountDetails.get(login);
  el('identity').hidden=!login;
  el('name').textContent=item?.nickname||'未提供昵称';
  el('wechat-id').textContent=item?.wechat_id||'未提供';
  el('raw-id').textContent=login||'';
 }
 const chosen=new Map();
 let listVersion=null;
 function paintChosen(){
  const menu=el('chosen-list');menu.replaceChildren();
  for(const [id,item] of chosen){
   const option=document.createElement('option');option.value=id;option.textContent=item.title;menu.append(option);
  }
 }
 function clearChosen(){listVersion=null;chosen.clear();paintChosen();el('selection-info').textContent='';}
 function summary(){paintChosen();el('selection-info').textContent='已勾选 '+chosen.size+' 个会话。';}
 function clearDirectory(){picker.setRows([]);picker.search.value='';picker.status.textContent='';}
 function clearPreview(){preview=null;el('warning').textContent='';el('preview').hidden=true;}
 function render(){
  el('actions').hidden=!state?.ready;
  el('load').disabled=!allowed||busy;
  el('platform').disabled=busy;
  el('detect').hidden=el('platform').value!=='wechat';
  el('detect').disabled=el('client-login').disabled=!allowed||busy;
  el('wechat-picker').hidden=!profiles;
  el('login').disabled=!allowed||busy;
  el('select').disabled=!allowed||busy||!el('login').value||el('login').value===profiles?.selected;
  el('directory').hidden=!profiles?.selected;
  for(const id of ['verify'])el(id).disabled=!allowed||busy||!chosen.size;
  el('read-list').disabled=!allowed||busy||!profiles?.selected;
  el('save-list').disabled=!allowed||busy||!listVersion;
  el('remove-one').disabled=!allowed||busy||!chosen.size;
  picker.setState(allowed&&!!profiles?.selected,busy);
  for(const [kind,label] of [['private','好友'],['group','群']]){
   const on=state?.categories?.[kind]?.enabled;
   el(kind).textContent=on?'关闭「允许所有'+label+'」':'允许所有'+label;
   el(kind).disabled=busy||!state?.ready;
  }
  el('confirm').disabled=busy||!preview;
 }
 function reset(){
  epoch++;busy=false;state=null;profiles=null;accountDetails.clear();el('login').replaceChildren();clearPreview();
  clearDirectory();clearChosen();picker.setRows([]);
  el('accounts').hidden=true;el('accounts').textContent='';paintIdentity(null);
  el('status').textContent=el('notice').textContent=el('account-notice').textContent='';render();
 }
 window.addEventListener('qiban-tab-change',({detail:{group,target}})=>{
  if(group!=='platforms'||target==='bilibili'||target===el('platform').value)return;
  el('platform').value=target;
  // Preserve the WeChat directory draft, but never carry an account's scope or confirmation into the other tab.
  epoch++;busy=false;state=null;clearPreview();
  el('status').textContent=el('notice').textContent=el('account-notice').textContent='';render();
 });
 function call(op,args){
  const request_id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(request_id);reject(Error('操作超时，请重新查看当前范围。'));},op.startsWith('wechat.')?60000:15000);
   pending.set(request_id,{resolve,reject,timer});
   window.chrome.webview.postMessage({action:'management',operation:op,arguments:args,request_id});
  });
 }
 async function work(job){
  if(!allowed||busy)return;
  const current=epoch;
  const notice=document.activeElement?.closest?.('#audience-panel')?el('account-notice'):document.activeElement?.closest?.('#audience-directory')?el('selection-info'):el('notice');
  const finish=window.QibanFeedback?.start(document.activeElement);
  busy=true;render();
  try{await job(()=>allowed&&epoch===current);}
  catch(error){if(epoch===current)notice.textContent=error.message;}
  finally{finish?.();if(epoch===current){busy=false;render();}}
 }
 el('platform').onchange=reset;
 el('client-login').onclick=()=>{if(allowed&&!busy)window.chrome.webview.postMessage({action:'wechat-client-login'});};
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='wechat-login')el('account-notice').textContent=data.message;
 });
 el('detect').onclick=()=>work(async live=>{
  if(el('platform').value!=='wechat')return;
  const status=el('accounts');
  profiles=null;accountDetails.clear();paintIdentity(null);clearDirectory();clearChosen();status.hidden=false;status.textContent='正在检测微信账号…';el('account-notice').textContent='';
  try{
   const result=await call('wechat.accounts',{});
   if(!live())return;
   profiles=result.profiles;
   const accounts=result.accounts;
   const picker=el('login');picker.replaceChildren();
   for(const login of new Set([...(profiles?.accounts||[]),...accounts])){
    const option=document.createElement('option');option.value=login;
    option.textContent=login+(accounts.includes(login)?'（已登录）':'（已保存）');picker.append(option);
   }
   if(profiles?.selected)picker.value=profiles.selected;
   status.textContent=profiles?.selected?'当前使用：'+profiles.selected:
    accounts.length?'请选择要使用的微信账号。':'请先在电脑微信登录，再点击检测。';
   const missing=[];
   for(const login of accounts){
    if(!live())return;
    el('account-notice').textContent='正在读取账号昵称和微信号…';
    try{
     const item=await call('wechat.account_details',{login});
     if(!live())return;
     if(item.login!==login)throw Error('账号资料不匹配');
     accountDetails.set(login,item);
     for(const option of picker.children)if(option.value===login)option.textContent=accountLabel(login)+'（已登录）';
    }catch(error){if(!live())return;missing.push(login);}
   }
   paintIdentity(profiles?.selected||picker.value);status.textContent=profiles?.selected?'当前使用':picker.value?'待选择':'请先在电脑微信登录，再点击检测。';
   el('account-notice').textContent=missing.length?'部分账号资料暂未读取成功，保留原始 ID；可重新检测。':'';
  }catch(error){if(live())status.textContent=error.message;}
 });
 el('login').onchange=()=>{el('accounts').textContent='待选择';paintIdentity(el('login').value);render();};
 el('select').onclick=()=>work(async live=>{
  if(!profiles||!el('login').value)return;
  const saved=await call('wechat.select',{login:el('login').value,expected:profiles.revision});
  if(!live())return;
  profiles=saved;state=null;clearPreview();clearDirectory();clearChosen();el('status').textContent='';
  el('accounts').textContent='当前使用';paintIdentity(saved.selected);
  el('account-notice').textContent='微信账号已保存。';
  setTimeout(()=>syncDirectory(),0);
 });
 const picker=new window.QibanContacts.Picker(el('contact-picker'),{
  selected:row=>chosen.has(row.id),toggle:(row,on)=>{if(on)chosen.set(row.id,{kind:row.kind,title:row.name||row.nickname||row.alias||row.id});else chosen.delete(row.id);summary();render();},sync:()=>syncDirectory()
 });
 async function syncDirectory(){
  return work(async live=>{
   picker.status.textContent='正在同步名单…';
   const active=await call('wechat.profiles',{});if(!live())return;
   if(profiles?.selected!==active.selected||profiles?.revision!==active.revision){clearChosen();picker.setRows([]);}
   profiles=active;el('login').replaceChildren();
   for(const login of active.accounts||[]){const option=document.createElement('option');option.value=login;option.textContent=accountLabel(login);el('login').append(option);}
   el('login').value=active.selected||'';paintIdentity(active.selected);render();
   if(!active.selected){picker.status.textContent='请先选择微信账号。';return;}
   try{
    const result=await call('wechat.directory_sync',{login:active.selected,expected:active.revision});if(!live())return;
    if(result.account?.login===active.selected){accountDetails.set(active.selected,result.account);paintIdentity(active.selected);for(const option of el('login').children)option.textContent=accountLabel(option.value);}
    if(!listVersion){const saved=await call('wechat.targets.saved',{login:active.selected,expected:active.revision});if(!live())return;
     listVersion=saved.version;for(const t of saved.targets)chosen.set(t.username,{kind:t.kind,title:t.username});}
    for(const row of result.items){if(chosen.has(row.id))chosen.get(row.id).title=row.name||row.nickname||row.alias||row.id;}
    picker.setRows(result.items);summary();picker.status.textContent='已同步 '+result.items.length+' 个联系人';
   }catch(error){if(live())picker.status.textContent=error.message;}
  });
 }
 window.QibanContacts.onEntry('wechat',()=>allowed,()=>{el('platform').value='wechat';void syncDirectory();});
 el('remove-one').onclick=()=>{
  const id=el('chosen-list').value;chosen.delete(id);
  summary();render();
 };
 el('read-list').onclick=()=>work(async live=>{
  const result=await call('wechat.targets.saved',{login:profiles.selected,expected:profiles.revision});
  if(!live())return;
  chosen.clear();listVersion=result.version;
  for(const {kind,username} of result.targets)chosen.set(username,{kind,title:picker.rows.find(r=>r.id===username)?.name||username});
  picker.render();paintChosen();el('selection-info').textContent='已读取回复名单，共 '+chosen.size+' 个会话。';
 });
 el('save-list').onclick=()=>work(async live=>{
  const targets=Array.from(chosen,([username,{kind}])=>({kind,username}));
  try{
   const result=await call('wechat.targets.save',{login:profiles.selected,expected:profiles.revision,
    version:listVersion,targets});
   if(!live())return;
   listVersion=result.version;el('selection-info').textContent='回复名单已保存，共 '+result.targets.length+' 个会话。';
  }catch(error){if(live())el('selection-info').textContent=error.message;}
 });
 el('verify').onclick=()=>work(async live=>{
  if(!profiles?.selected||!chosen.size)return;
  const targets=Array.from(chosen,([username,{kind}])=>({kind,username}));
  el('selection-info').textContent='正在核验 '+targets.length+' 个会话…';
  try{
   const result=await call('wechat.targets.verify',{login:profiles.selected,expected:profiles.revision,targets});
   if(!live())return;
   const missing=result.unavailable.map(id=>chosen.get(id).title);
   el('selection-info').textContent='通过核验 '+result.verified+' 个。'+
    (missing.length?'以下会话的好友或群关系未通过核验：'+missing.join('、'):'');
  }catch(error){if(live())el('selection-info').textContent=error.message;}
 });
 el('load').onclick=()=>work(async live=>{
  clearPreview();state=null;el('notice').textContent='';
  const result=await call('audience.status',{platform:el('platform').value});
  if(!live())return;
  state=result;el('status').textContent=result.ready?'当前账号：'+(result.display_account||result.account):result.message;
 });
 for(const kind of ['private','group']){
  el(kind).onclick=()=>work(async live=>{
   if(!state?.ready)return;
   clearPreview();
   const value=await call('audience.preview',{platform:el('platform').value,kind,enabled:!state.categories[kind].enabled,account:state.account});
   if(!live())return;
   preview=value;el('preview').hidden=false;el('warning').textContent=value.message;
   el('confirm').textContent=value.button;el('notice').textContent='';
  });
 }
 el('cancel').onclick=()=>{clearPreview();render();el('notice').textContent='已取消，本次修改未生效。';};
 el('confirm').onclick=()=>work(async live=>{
  if(!preview)return;
  const value=preview;clearPreview();
  const saved=await call('audience.save',{token:value.token});
  if(!live())return;
  state.categories[value.scope.kind]=saved;
  el('notice').textContent=saved.enabled?'已开启持续授权。':'已恢复手动回复范围。';
 });
 window.addEventListener('qiban-management-state',({detail})=>{
  const now=detail.ready&&!detail.locked;
  document.getElementById('audience-panel').hidden=document.getElementById('management-panel').hidden;
  document.getElementById('audience-scope-panel').hidden=!now;
  if(now!==allowed){allowed=now;reset();}
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  const job=pending.get(data.request_id);
  if(data.type==='management'&&job){
   clearTimeout(job.timer);pending.delete(data.request_id);
   if(data.response.ok)job.resolve(data.response.result);else job.reject(Error(data.response.message));
  }else if(data.type==='management_error'){
   for(const job of pending.values()){clearTimeout(job.timer);job.reject(Error(data.message));}
   pending.clear();
  }
 });
 reset();
})();
