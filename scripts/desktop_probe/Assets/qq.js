(()=>{
 'use strict';
 const el=id=>document.getElementById(id),pending=new Map();
 let allowed=false,state=null,submitting=false,refreshing=false,refreshQueued=false,epoch=0,signature='';
 let targetRevision=null,managementSession=null;
 let followingLogin=false;
 let operationArea='login';
 const picker=new window.QibanContacts.Picker(el('qq-contact-picker'),{selected:row=>numbers(row.kind==='group'?'qq-group-targets':'qq-private-targets').includes(row.id),toggle:(row,on)=>{const id=row.kind==='group'?'qq-group-targets':'qq-private-targets';const values=new Set(numbers(id));if(on)values.add(row.id);else values.delete(row.id);el(id).value=[...values].join('\n');render();},sync:()=>perform('directory'),groupSettings:row=>window.QibanGroupModes?.open('qq',row.id)});
 let entrySync=false;
 const visible=()=>document.body.dataset.navPage==='platforms'&&document.body.dataset.tabPlatforms==='qq';
 for(const event of ['qiban-nav-change','qiban-tab-change'])window.addEventListener(event,()=>{if(!visible())entrySync=false;});
 window.QibanContacts.onEntry('qq',()=>allowed,async()=>{entrySync=true;await refresh();if(allowed&&!state?.selected&&!state?.busy&&!state?.running)void perform('refresh');});
 const numbers=id=>el(id).value.split(/[\s,，]+/u).filter(Boolean);
 function editedTargets(){return {private_targets:numbers('qq-private-targets'),groups:numbers('qq-group-targets')};}
 function previewMatches(){const value=editedTargets();return state?.preview&&
  JSON.stringify(value.private_targets)===JSON.stringify(state.preview.private_targets)&&
  JSON.stringify(value.groups)===JSON.stringify(state.preview.groups);}
 function call(operation,args={}){
  const id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(id);const error=new Error('连接未及时返回，请重新检查。');error.timedOut=true;reject(error);},12000);
   pending.set(id,{resolve,reject,timer});
   window.chrome.webview.postMessage({action:'management',operation:'qq.'+operation,arguments:args,request_id:id});
  });
 }
 function render(){
  window.QibanGroupModes?.setAccount('qq',allowed?state?.selected?.uin:null);
  const busy=submitting||!!state?.busy,accounts=allowed?(state?.accounts||[]):[];
  const passwordMode=el('qq-login-method').value==='password';
  el('qq-password-fields').hidden=!passwordMode;
  el('qq-login').textContent=passwordMode?'密码登录 QQ':'扫码登录 QQ';
  for(const id of ['qq-login-method','qq-login-account','qq-login-password'])el(id).disabled=!allowed||busy;
  const message=!allowed?'解锁后可查看 QQ 账号。':state?.message||'尚未检查登录状态。';
  el('qq-status').textContent=operationArea==='login'?message:'';

  if(operationArea==='targets')el('qq-targets-notice').textContent=message;
  for(const [area,id] of [['login','qq-login'],['targets','qq-targets-verify']])window.QibanFeedback?.mark(el(id),allowed&&busy&&operationArea===area);
  el('qq-loading').hidden=!allowed||!busy||operationArea!=='login'||['qr_ready','qr_scanned'].includes(state?.state);
  el('qq-current').textContent=allowed&&state?.selected?`已保存：${state.selected.label}（${state.selected.uin}）`:'';
  const next=JSON.stringify(accounts);
  if(signature!==next){
   const previous=el('qq-selection').value;el('qq-selection').replaceChildren();
   for(const row of accounts){const option=document.createElement('option');option.value=row.uin;
    option.textContent=`${row.label}（${row.uin}）${row.can_quick_login?'':' · 需要重新扫码'}`;option.disabled=!row.can_quick_login;el('qq-selection').append(option);}
   const choice=accounts.find(a=>a.uin===state?.authenticated_uin&&a.can_quick_login)||accounts.find(a=>a.uin===previous&&a.can_quick_login)||accounts.find(a=>a.uin===state?.selected?.uin&&a.can_quick_login)||accounts.find(a=>a.can_quick_login);
   el('qq-selection').value=choice?.uin||'';signature=next;
  }
  const png=allowed&&['qr_ready','qr_scanned'].includes(state?.state)?state.png_base64:null;
  el('qq-qr-area').hidden=!png;
  if(png){const src='data:image/png;base64,'+png;if(el('qq-qr').src!==src)el('qq-qr').src=src;}
  else el('qq-qr').removeAttribute('src');
  el('qq-selection-area').hidden=accounts.length===0;
  for(const name of ['login','refresh'])el('qq-'+name).disabled=!allowed||busy;
  el('qq-cancel').disabled=!allowed||submitting||!state?.busy;
  el('qq-selection').disabled=!allowed||busy;
  el('qq-select').disabled=!allowed||busy||!el('qq-selection').value;
  el('qq-targets-area').hidden=!allowed||!state?.selected;
  const revision=allowed&&state?.selected?state.revision:null;
  if(revision!==targetRevision){targetRevision=revision;
   el('qq-private-targets').value=state?.targets?.private_targets.join('\n')||'';
   el('qq-group-targets').value=state?.targets?.groups.join('\n')||'';
  }
  for(const id of ['qq-private-targets','qq-group-targets','qq-targets-verify'])el(id).disabled=!allowed||busy||!state?.selected;
  el('qq-targets-save').disabled=!allowed||busy||!previewMatches();
  el('qq-targets-summary').textContent=allowed&&state?.targets?`已保存 ${state.targets.private_targets.length} 个好友、${state.targets.groups.length} 个群。${state.targets.authorized?'':'回复授权已关闭，请重新核验并保存。'}`:'';
  el('qq-targets-preview').textContent=allowed&&previewMatches()?`核验通过：${state.preview.private_targets.length} 个好友、${state.preview.groups.length} 个群。`:'';
  el('qq-cancel').textContent=state?.state==='loading_directory'?'取消读取':state?.state==='checking_targets'?'取消核验':'取消登录';
  const directory=allowed?state?.directory:null;
  picker.setRows([...(directory?.private_targets||[]).map(r=>({id:r.id,name:r.label,nickname:r.nickname,kind:'private'})),...(directory?.groups||[]).map(r=>({id:r.id,name:r.label,kind:'group'}))]);
  picker.setState(allowed&&!!state?.selected,busy);picker.status.textContent=operationArea==='directory'?message:'';

 }
 async function refresh(){
  if(!allowed)return;
  if(refreshing){refreshQueued=true;return;}
  refreshing=true;const current=epoch;
  try{const value=await call('status');if(allowed&&current===epoch){
   if(followingLogin){el('qq-notice').textContent='';followingLogin=!!value.busy;}
   state=value;el('qq-refresh').hidden=true;render();}}
  catch(error){if(allowed&&current===epoch){el('qq-notice').textContent=error.message;el('qq-refresh').hidden=false;el('qq-refresh').textContent='重试';}}
  finally{refreshing=false;if(entrySync&&state?.running)entrySync=false;if(entrySync&&allowed&&!submitting&&!state?.busy&&state?.selected){entrySync=false;void perform('directory');}if(refreshQueued){refreshQueued=false;void refresh();}}
 }
 async function perform(operation){
  if(!allowed||submitting)return;submitting=true;const current=++epoch;
  if(operation!=='cancel')operationArea=operation==='directory'?'directory':operation.startsWith('targets.')?'targets':'login';render();
  const notice=el(operation==='directory'||operation.startsWith('targets.')?'qq-targets-notice':'qq-notice');
  try{
   notice.textContent='';
   const args=operation==='select'?{account:el('qq-selection').value,expected:state.revision}:
    operation==='targets.verify'?{...editedTargets(),expected:state.revision}:
    operation==='targets.save'?{token:state.preview.token,expected:state.revision}:
    operation==='directory'?{expected:state.revision}:{};
   if(operation==='login'&&el('qq-login-method').value==='password'){
    operation='password';args.account=el('qq-login-account').value.trim();args.password=el('qq-login-password').value;
    el('qq-login-password').value='';
   }
   const login=operation==='login'||operation==='password';
   try{await call(operation,args);if(login&&allowed&&current===epoch)followingLogin=true;}
   catch(error){if(login&&error.timedOut&&allowed&&current===epoch)followingLogin=true;throw error;}
   finally{if('password' in args)args.password='';}
   if(!allowed||current!==epoch)return;
   await refresh();
   if(operation==='select'){notice.textContent='账号已保存。';entrySync=document.body.dataset.navPage==='platforms'&&document.body.dataset.tabPlatforms==='qq';}
   if(operation==='targets.save')notice.textContent='回复会话已保存。';
   if(operation==='targets.save'||operation==='directory')window.QibanGroupModes?.refresh('qq');
   if(operation==='cancel')notice.textContent='已请求取消，正在结束当前 QQ 账号操作。';
  }catch(error){if(allowed&&current===epoch)notice.textContent=error.message;}
  finally{submitting=false;render();if(entrySync&&allowed&&!state?.busy&&state?.selected){entrySync=false;void perform('directory');}}
 }
 for(const name of ['login','refresh','cancel','select'])el('qq-'+name).onclick=()=>perform(name);
 for(const name of ['verify','save'])el('qq-targets-'+name).onclick=()=>perform('targets.'+name);
 for(const name of ['qq-private-targets','qq-group-targets'])el(name).oninput=render;
 el('qq-selection').onchange=render;
 el('qq-login-method').onchange=()=>{el('qq-login-password').value='';render();};
 window.addEventListener('qiban-management-state',({detail})=>{
  const next=detail.ready&&!detail.locked,session=next?detail.roomSession:null;
  const switched=allowed&&next&&managementSession!==session;
  const changed=allowed!==next||switched;allowed=next;managementSession=session;
  el('qq-panel').hidden=el('management-panel').hidden;
  if(!allowed||switched){el('qq-login-password').value='';followingLogin=false;state=null;epoch++;entrySync=!!switched&&visible();el('qq-notice').textContent=el('qq-targets-notice').textContent='';}
  render();if(allowed&&changed)void refresh();
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management'){
   const item=pending.get(data.request_id);if(!item)return;clearTimeout(item.timer);pending.delete(data.request_id);
   if(data.response.ok)item.resolve(data.response.result);else item.reject(new Error(data.response.message||'QQ 账号操作未完成。'));
  }
  if(data.type==='management_error'){for(const item of pending.values()){clearTimeout(item.timer);item.reject(new Error(data.message));}pending.clear();}
 });
 setInterval(()=>{if(allowed&&!submitting&&(followingLogin||state?.busy))void refresh();},500);
 render();
})();
