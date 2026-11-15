(()=>{
 'use strict';
 const element=id=>document.getElementById(id),pending=new Map();
 let allowed=false,refreshing=false,last=0,state=null,openWhenReady=false;
 let submitting=false,completion=null,revision=0,accountSignature='',lastActive=null;
 function feedback(title,message){
  element('model-notice').textContent=message;
  element('model-feedback-title').textContent=title;element('model-feedback-message').textContent=message;
  if(!element('model-feedback').open)element('model-feedback').showModal();
 }
 element('model-feedback-close').onclick=()=>element('model-feedback').close();
 function call(operation,args={}){
  const id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(id);reject(new Error('连接未及时返回，请重新检查状态。'));},12000);
   pending.set(id,{resolve,reject,timer});
   window.chrome.webview.postMessage({action:'management',operation:'model.'+operation,arguments:args,request_id:id});
  });
 }
 function render(){
  const busy=submitting||!!completion||!!state?.busy;
  window.QibanFeedback?.mark(element('model-login'),allowed&&busy);
  const multiple=allowed&&!!state?.multi_account,selection=element('model-selection');
  element('model-accounts').hidden=!multiple||state.accounts.length===0;
  const signature=multiple?JSON.stringify([state.accounts,state.active_account]):'';
  if(signature!==accountSignature){
   const previous=selection.value;selection.replaceChildren();
   for(const account of multiple?state.accounts:[]){
    const option=document.createElement('option');option.value=account.id;
    option.textContent=account.label+(account.active?' · 当前':'')+(account.signed_in?'':' · 已退出');
    selection.append(option);
   }
   selection.value=(multiple&&lastActive===state.active_account&&state.accounts.some(a=>a.id===previous))?previous:(state?.active_account||'');
   lastActive=state?.active_account||null;
   accountSignature=signature;
  }
  selection.disabled=!multiple||busy;
  element('model-add').disabled=!multiple||busy;
  element('model-select').disabled=!multiple||busy||!selection.value||(selection.value===state.active_account&&!state.handoff_pending);
  element('model-select').textContent=state?.handoff_pending?'使用所选账号继续交接':'使用所选账号';
  element('model-login').textContent=state?.authentication_route==='siwc_validation'?'继续 ChatGPT 登录':'登录 ChatGPT';
  element('model-status').textContent=!allowed?'解锁后可查看模型账号。':state?.handoff_pending&&!busy?'账号交接未完成，任务保持停止。请点击“使用所选账号继续交接”。':state?.message||'正在读取模型状态……';
  const current=multiple?state.accounts.find(a=>a.id===state.active_account)?.label:null;
  element('model-account').textContent=allowed&&state?.state==='authenticated'?`${current||state.email||'ChatGPT 账号'} · ${state.plan||'订阅类型未知'}`:'';
  for(const name of ['refresh','login','logout'])element('model-'+name).disabled=!allowed||busy||(name==='logout'&&state?.state!=='authenticated');
  element('model-cancel').disabled=!allowed||submitting||!!completion||!state?.busy;
  element('model-open').disabled=!allowed||submitting||!!completion||!state?.can_open_login;
 }
 function completed(){
  if(!completion||state.busy)return;
  const operation=completion;completion=null;
  if(operation==='cancel'){
   if(state.state==='cancelled')feedback('账号操作已取消',state.message);
   else if(state.state==='authenticated'&&state.renewal_status)feedback('续期操作结果',state.message);
   else if(state.state==='authenticated')feedback('登录已经完成','登录已在取消前完成，当前账号仍为登录状态。');
   else feedback('账号操作提示',state.state==='signed_out'?'当前未登录，没有仍在等待的登录请求。':state.message);
  }else if(operation==='logout'){
   if(state.state==='signed_out')feedback('已退出本应用账号',state.remote_revocation_confirmed===false?state.message:(state.multi_account?'已退出当前账号，其他账号和角色资料不变。':'已退出栖伴中的 ChatGPT 账号，角色资料保持不变。'));
   else feedback('退出账号提示',state.message);
  }else if(operation==='select'){
   feedback(state.state==='error'?'账号切换未完成':'当前账号已切换',state.message);
  }
 }
 async function refresh(){
  if(!allowed||refreshing)return;
  refreshing=true;
  const expected=revision;
  try{
   const value=await call('status');
   if(!allowed||expected!==revision)return;
   state=value;completed();render();
   if(openWhenReady&&state.can_open_login){openWhenReady=false;await call('open');}
   if(!state.busy)openWhenReady=false;
   last=Date.now();
  }catch(error){element('model-notice').textContent=error.message;}
  finally{refreshing=false;}
 }
 async function perform(operation){
  if(submitting||completion)return;
  submitting=true;revision++;render();
  try{
   if(operation!=='refresh'&&operation!=='open')element('model-notice').textContent='';
   if(operation==='login'||operation==='add')openWhenReady=true;
   if(operation==='cancel')openWhenReady=false;
   const args=operation==='select'?{account_id:element('model-selection').value,expected:state.accounts_revision}:{};
   const result=await call(operation,args);
   if(!allowed)return;
   if(operation==='cancel'){
    if(result.cancel_requested){completion='cancel';element('model-notice').textContent='正在取消账号操作，请稍候……';}
    else feedback('当前没有待取消的操作','当前没有正在进行的账号操作。');
   }
   if(operation==='logout'){completion='logout';element('model-notice').textContent='正在退出本应用账号……';}
   if(operation==='select'){completion='select';element('model-notice').textContent='正在停止全部任务并切换账号……';}
   await refresh();
  }catch(error){openWhenReady=false;completion=null;if(allowed)feedback('模型账号操作提示',error.message);}
  finally{submitting=false;render();}
 }
 for(const name of ['refresh','login','logout','cancel','open','add','select'])element('model-'+name).onclick=()=>perform(name);
 element('model-selection').onchange=render;
 window.addEventListener('qiban-management-state',({detail})=>{
  allowed=detail.ready&&!detail.locked;
  element('model-panel').hidden=element('management-panel').hidden;
  if(!allowed){state=null;openWhenReady=false;completion=null;revision++;element('model-notice').textContent='';element('model-feedback').close();}
  render();if(allowed&&Date.now()-last>1000)void refresh();
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management'){
   const item=pending.get(data.request_id);if(!item)return;
   clearTimeout(item.timer);pending.delete(data.request_id);
   if(data.response.ok)item.resolve(data.response.result);else item.reject(new Error(data.response.message||'模型账号操作未完成。'));
  }
  if(data.type==='management_error'){
   for(const item of pending.values()){clearTimeout(item.timer);item.reject(new Error(data.message));}pending.clear();
  }
 });
 render();
})();
