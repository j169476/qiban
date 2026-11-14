'use strict';
// Uninstall preparation page. It never deletes anything and never starts the installer's own
// uninstaller by itself: every step below is one request to the window, which answers with one
// state document. "继续卸载" stays disabled until this run really backed up, or until the person
// explicitly chose to skip a backup; a cancelled or failed attempt leaves the page where it was.
// Nothing here loads the full management area, and no step starts a platform or a model turn.
(()=>{
 const element=id=>document.getElementById(id), pending=new Map();
 const steps=['prepare','backup','confirm'];
 let state=null,ready=false,backend='stopped',locked=null,exporting=false,continuing=false,busy=false;
 const notice=text=>{element('backup-notice').textContent=text;};
 const unlockNotice=text=>{element('unlock-notice').textContent=text;};
 const portableNotice=text=>{element('portable-notice').textContent=text;};
 function call(operation,args={},timeout=15000){
  const id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(id);reject(new Error('操作未及时返回，请重新读取状态后再试。'));},timeout);
   pending.set(id,{resolve,reject,timer});
   window.chrome.webview.postMessage({action:'management',operation,arguments:args,request_id:id});
  });
 }
 function send(action){window.chrome.webview.postMessage({action});}
 const statusLines={
  none:'尚未备份。可以先保存备份，或明确选择跳过备份。',
  running:'正在保存备份，请稍候……',
  succeeded:'本次备份已完成。',
  failed:'本次备份没有完成；当前资料未改变。',
  skipped:'本次已选择跳过备份。'};
 function stepFor(){
  if(!state)return 'prepare';
  if(state.backup==='succeeded'||state.backup==='skipped')return 'confirm';
  return 'backup';
 }
 function render(){
  const s=state||{},registered=s.registered===true,allowed=s.continue_refusal===''&&!busy&&!exporting&&!continuing;
  const finish=s.backup==='succeeded'||s.backup==='skipped';
  element('uninstall-service').textContent=backend==='ready'?'准备后台已连接，可以保存备份。'
   :backend==='starting'?'正在启动准备后台……'
   :backend==='stopping'?'正在停止准备后台……'
   :backend==='failed'?'准备后台没有启动；可以重试，或选择跳过备份继续。'
   :'准备后台未启动；只有保存备份时才需要它。';
  element('uninstall-summary').textContent=registered
   ? '卸载会由安装器登记的卸载程序执行：它只删除安装器登记的程序文件和快捷方式。个人资料、备份与凭据都保留。'
   : '没有找到本机登记的卸载程序，因此本次不会自动卸载；下面的说明告诉你可以怎么做。';
  element('uninstall-entry').textContent=registered
   ? `卸载程序：${s.uninstaller||''}`
   : (s.guidance||'');
  element('uninstall-retention').textContent=`个人资料目录：${s.profile||''}`;
  element('uninstall-backup-location').textContent=`本机备份位置：${s.backup_folder||'准备后台连接后显示'}；便携备份由你选择保存位置。`;
  if(s.backup_available===false)element('uninstall-backup-location').textContent=
   `${element('uninstall-backup-location').textContent}\n${s.backup_reason||'本机还没有可备份的资料。'}`;
  const blocked=!s.backup_available||busy||exporting||continuing||locked===true;
  element('backup-create').disabled=blocked||s.backup==='running';
  element('backup-create').textContent=s.backup==='running'?'正在保存备份……':'保存本机加密备份';
  element('unlock-area').hidden=locked!==true;
  element('unlock-password').disabled=locked!==true||busy;
  element('unlock').disabled=locked!==true||busy;
  element('portable-password').disabled=blocked||exporting;
  element('portable-confirm').disabled=blocked||exporting;
  element('portable-export').disabled=blocked||exporting;
  element('uninstall-skip').disabled=busy||exporting||continuing||finish||s.backup==='running'||!registered;
  element('uninstall-skip').textContent=s.backup==='skipped'?'已选择跳过备份':'跳过备份';
  element('uninstall-continue').disabled=!allowed||busy||continuing;
  element('uninstall-continue').textContent=continuing?'正在关闭应用……':'继续卸载…';
  element('uninstall-result').textContent=registered
   ? (finish?'可以继续卸载；继续后会先正常关闭应用与数据库，再打开安装器的卸载程序。'
     :s.backup==='running'?'备份完成后才会显示结果。'
     :'还不能继续卸载：请先完成备份，或明确选择跳过备份。')
   : '保存备份后可退出本窗口，再手动删除免安装程序目录。';
  element('uninstall-status').textContent=`${statusLines[s.backup||'none']||statusLines.none}`
   +(s.backup==='succeeded'&&s.backup_name?`（${s.backup_name}）`:'')
   +(s.backup==='failed'&&s.backup_detail?`\n${s.backup_detail}`:'')
   +(locked===true?'\n栖伴已锁定，备份前需要先解锁。':'');
  document.body.dataset.uninstall=registered?(finish?'ready':'preparing'):'unregistered';
  const current=stepFor();
  for(const step of steps)element('uninstall-nav').querySelector(`[data-step="${step}"]`)
   .setAttribute('aria-current',step===current?'page':'false');
 }
 function apply(next){
  state=next;
  if(state.backup==='failed'&&state.backup_detail)notice(state.backup_detail);
  if(state.backup==='running')notice('正在保存备份，请稍候……');
  if(state.backup==='succeeded')notice(`备份已保存：${state.backup_name||''}`);
  if(state.backup==='skipped')notice('已选择跳过备份。');
  render();
 }
 async function refreshLock(){
  if(!ready||!window.chrome.webview)return;
  try{const value=await call('session.status');locked=value.locked===true;render();}
  catch(error){unlockNotice(error.message);}
 }
 function password(){
  const value=element('portable-password').value,confirm=element('portable-confirm').value;
  element('portable-password').value='';element('portable-confirm').value='';
  if(value.length<16||value.length>256)throw new Error('独立恢复口令需为 16 至 256 个字符。');
  if(/\p{Cc}/u.test(value))throw new Error('独立恢复口令不能包含控制字符。');
  if(value!==confirm)throw new Error('两次输入的独立恢复口令不一致，请重新填写。');
  return value;
 }
 element('uninstall-exit').onclick=()=>{if(!continuing)send('exit');};
 element('backup-create').onclick=()=>{
  if(busy||exporting||continuing)return;
  busy=true;notice('正在保存备份，请稍候……');render();send('uninstall-backup');
 };
 element('uninstall-skip').onclick=()=>{
  if(busy||exporting||continuing||!state||state.registered!==true)return;
  busy=true;notice('已选择跳过备份；需要时仍可以先保存备份。');render();send('uninstall-skip');
 };
 element('portable-export').onclick=async()=>{
  if(busy||exporting||continuing)return;
  let secret='';
  try{secret=password();}catch(error){portableNotice(error.message);return;}
  exporting=true;portableNotice('请先在窗口中选择保存位置，再完成导出。');render();
  try{
   const result=await special('portable-export',{password:secret});
   portableNotice(`已导出 ${result.backup||'便携备份'} 与 ${result.key_file||'独立恢复密钥'}。请把两个文件放在一起保管。`);
  }catch(error){portableNotice(error.message);}
  finally{exporting=false;render();}
 };
 function special(action,args){
  const id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(id);reject(new Error('操作未及时返回，请重新导出。'));},250000);
   pending.set(id,{resolve,reject,timer});
   window.chrome.webview.postMessage({action,request_id:id,...args});
  });
 }
 element('unlock').onclick=async()=>{
  const value=element('unlock-password').value;element('unlock-password').value='';
  unlockNotice('');
  try{const result=await call('session.unlock',{password:value});locked=result.locked===true;unlockNotice(locked?'密码不正确。':'已解锁，可以保存备份。');}
  catch(error){unlockNotice(error.message);}
  render();
 };
 element('uninstall-continue').onclick=()=>{
  if(!state||continuing||element('uninstall-continue').disabled)return;
  const backed=state.backup==='succeeded';
  element('uninstall-confirm-text').textContent=(backed
    ? `本次备份已完成${state.backup_name?`（${state.backup_name}）`:''}。`
    : '本次没有备份，已经选择跳过备份。')
   +'\n\n继续后会先正常关闭栖伴与数据库，然后打开安装器的卸载程序。'
   +'\n卸载程序只删除安装器登记的程序文件和快捷方式；个人资料、备份与凭据保留。'
   +'\n\n确定继续卸载吗？';
  if(!element('uninstall-confirm').open)element('uninstall-confirm').showModal();
 };
 element('uninstall-confirm-no').onclick=()=>element('uninstall-confirm').close();
 element('uninstall-confirm-yes').onclick=()=>{
  element('uninstall-confirm').close();
  if(!state||continuing||element('uninstall-continue').disabled)return;
  continuing=true;busy=true;render();send('uninstall-continue');
 };
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='uninstall'){busy=data.state?.backup==='running';apply(data.state||{});if(state.backup==='failed')void refreshLock();return;}
  if(data.type==='service'){backend=data.state||'stopped';ready=data.ready===true;render();
   if(ready&&locked===null)void refreshLock();
   if(!ready)locked=null;
   return;}
  if(data.type==='management'){
   const job=pending.get(data.request_id);if(!job)return;clearTimeout(job.timer);pending.delete(data.request_id);
   if(data.response&&data.response.ok)job.resolve(data.response.result);
   else job.reject(new Error((data.response&&data.response.message)||'操作未完成。'));
   return;}
  if(data.type==='management_error'){for(const job of pending.values()){clearTimeout(job.timer);job.reject(new Error(data.message));}pending.clear();}
 });
 window.chrome.webview.postMessage({action:'uninstall-status'});
})();
