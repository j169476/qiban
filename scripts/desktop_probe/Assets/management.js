'use strict';
(()=>{
 const element=id=>document.getElementById(id), pending=new Map();
 let ready=false,refreshing=false,recovering=false,identity=null,persona=null,windowsUser='当前 Windows 用户';
 let accessEpoch=0,locking=false;
 let backupBusy=false,backupPreview=null;
 let portableBusy=false,portablePreview=null,portableEpoch=0,portableKind='portable';
 let preservedItems=[],preservedCursor='',preservedSelection='';
 let inboxPage=1,inboxPages=1,inboxSequence=0,inboxSnapshot="",inboxBusy=false,confirmDelete=false;
 let inboxItems=[],inboxSelected=new Set();
 let runtimeRefreshing=false,lastRuntimeRefresh=0,observationPaused=false,observationRevision=0,lastRender='',refreshAfterAction=false;
 let profileBusy=false,profileDirty=false,profileEpoch=0;
 const profileFields=['persona-name','persona-description','persona-speaking','persona-interests'];
 function profileControls(){
  const usable=ready&&identity&&!identity.locked&&!profileBusy;
  for(const id of profileFields)element(id).disabled=!usable||!persona;
  element('save-persona').disabled=!usable||!persona||!profileDirty;
  element('reload-persona').disabled=!usable||profileDirty;
  element('discard-persona').disabled=!usable||!profileDirty;
  element('profile-draft').textContent=profileDirty?'有未保存的资料。保存或放弃后可重新读取。':'';
 }
 function fillProfile(){
  element('persona-name').value=persona?.data.name||'';
  element('persona-description').value=persona?.data.core.description||'';
  element('persona-speaking').value=(persona?.data.core.speaking_style||[]).join('\n');
  element('persona-interests').value=(persona?.data.interests||[]).join('\n');
  element('profile-area').dataset.loaded=String(!!persona);profileDirty=false;profileControls();
 }
 const notice=text=>{element('management-notice').textContent=text;};
 const recoveryNotice=text=>{element('recovery-feedback').textContent=text;};
 function feedback(title,text){
  element('feedback-title').textContent=title;element('feedback-message').textContent=text;
  if(!element('management-feedback').open)element('management-feedback').showModal();
 }
 element('feedback-close').onclick=()=>element('management-feedback').close();
 function call(operation,args={},special=null){
  const id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(id);reject(new Error('操作未及时返回，请先重新读取状态，不要重复确认恢复。'));},nativeTimeouts[special]??(operation.startsWith('backups.')?130000:12000));
   pending.set(id,{resolve,reject,timer});
   window.chrome.webview.postMessage(special?{action:special,request_id:id,...args}:{action:'management',operation,arguments:args,request_id:id});
  });
 }
 // Deadlines stay outside the window's own budgets: begin, the Windows verification and
 // complete each get 240/180/240 seconds inside the window, so the page waits a little
 // longer than that whole budget and never cuts a real reply off early. Cancel only needs
 // the window's short cancel bound plus the stop that follows it.
 const nativeTimeouts={'restore-backup':310000,'recover-lock':185000,
  'portable-export':250000,'portable-preview':250000,'portable-restore':700000,'portable-cancel':70000,
  'portable-preserved-list':60000,'portable-preserved-preview':250000};
 function render(){
  const locked=locking||!identity||identity.locked,enabled=!!identity?.lock_enabled;
  element('identity-status').textContent=!ready?'后台尚未就绪':`${windowsUser} · ${locked?'栖伴已锁定':enabled?'栖伴已解锁':'已识别本机管理员，无需再次登录'}`;
  element('unlock-area').hidden=!ready||!locked;
  element('recovery-area').hidden=ready&&!!identity&&!enabled;
  element('recovery-status').textContent=!ready?'后台未连接，密码恢复暂不可用。请先在上方启动应用后台。':!identity?'正在读取应用锁状态……':'';
  for(const id of ['recover-lock','recovery-password','recovery-confirm'])element(id).disabled=recovering||!ready||!identity||!enabled;
  element('recover-lock').textContent=recovering?'正在验证并重设……':'通过 Windows 验证并重设';
  element('enable-lock').disabled=!ready||locked||enabled;
  element('lock-now').disabled=!ready||locked||!enabled;
  element('disable-lock').disabled=!ready||locked||!enabled;
  element('lock-password').disabled=!ready||locked;
  if(locked||!ready){profileEpoch++;profileBusy=false;persona=null;fillProfile();element('profile-notice').textContent='';}
  profileControls();
  element('management-panel').dataset.locked=String(locked);
  element('management-unavailable').hidden=ready||!element('management-panel').hidden;
  // LAYOUT-172 · 页面壳把身份/资料/锁/通知/备份预览拆到不同栏位，这些容器仍然只在后台
  // 就绪时出现，和原来的 management-panel 同步 hidden；显示层不新增任何可用性变化。
  for(const id of ['backup-panel','lock-panel','profile-panel','notification-panel','backup-rail-local','backup-rail-source'])element(id).hidden=element('management-panel').hidden;
  for(const id of ['backup-create','backup-refresh','backup-selection','backup-preview'])element(id).disabled=!ready||locked||backupBusy||recovering;
  element('backup-restore').disabled=!ready||locked||backupBusy||recovering||!backupPreview;
  if(locked||!ready){backupPreview=null;element('backup-selection').replaceChildren();element('backup-effect').textContent='';element('backup-notice').textContent='';}
  // Locking or losing the backend drops the portable preview and bumps the epoch, so a
  // late reply can neither re-enable the confirm button nor re-fill the password fields.
  if(locked||!ready)clearPortable();
  const portableBlocked=!ready||locked||recovering||portableBusy;
  for(const id of ['portable-export','portable-import','portable-password','portable-confirm'])element(id).disabled=portableBlocked;
  element('portable-restore').disabled=portableBlocked||!portablePreview;
  element('portable-cancel').disabled=portableBlocked||!portablePreview;
  renderPortableSource();
  element('preserved-list').disabled=portableBlocked;
  element('preserved-more').disabled=portableBlocked||!preservedCursor;
  element('preserved-preview').disabled=portableBlocked||!preservedSelection;
  element('runtime-panel').hidden=element('management-panel').hidden;
  element('runtime-refresh').disabled=!ready||locked;
  element('runtime-observe').disabled=!ready||locked;
  element('runtime-stop-all').disabled=!ready||locked;
  if(locked||!ready){resetInbox();observationPaused=false;observationRevision++;lastRender='';element('runtime-observe').textContent='暂停观察';element('monitor-state').textContent=!ready?'等待连接':identity?'已锁定':'正在读取状态';element('monitor-detail').textContent='';paintMonitor('idle',true);window.QibanHouse?.clear();element('runtime-platforms').replaceChildren();element('notification-items').replaceChildren();element('notification-title').textContent='应用通知';}
  window.dispatchEvent(new CustomEvent('qiban-management-state',{detail:{ready,locked,
   roomSession:ready&&!locked?identity.room_session:null,lockEnabled:enabled}}));
 }
 async function loadProfile(force=false){
  if(profileBusy||profileDirty&&!force||!ready||!identity||identity.locked)return;
  const token=profileEpoch;profileBusy=true;profileControls();
  try{const value=await call('config.read',{kind:'persona'});if(token===profileEpoch){persona=value;fillProfile();}}
  catch(error){if(token===profileEpoch)throw error;}
  finally{if(token===profileEpoch){profileBusy=false;profileControls();}}
 }
 async function refresh(){
  if(refreshing||!ready)return;
  refreshing=true;
  const epoch=accessEpoch;
  try{const value=await call('session.status');if(epoch!==accessEpoch||locking)return;identity=value;render();if(!identity.locked){await loadProfile();await loadBackups();await loadRuntime();}}
  catch(error){notice(error.message);}
  finally{refreshing=false;}
 }
 async function perform(work){try{notice('');await work();}catch(error){notice(error.message);}}
 for(const id of profileFields)element(id).oninput=()=>{profileDirty=true;element('profile-notice').textContent='';profileControls();};
 element('reload-persona').onclick=async()=>{try{await loadProfile();}catch(error){element('profile-notice').textContent=error.message;}};
 element('discard-persona').onclick=()=>{if(profileBusy)return;fillProfile();element('profile-notice').textContent='已放弃未保存的资料。';};
 element('save-persona').onclick=async()=>{
  if(!persona||!profileDirty||profileBusy||!ready||identity?.locked)return;
  const lines=(id,original)=>element(id).value===original.join('\n')?original:element(id).value.split(/\r?\n/).map(value=>value.trim()).filter(Boolean);
  const speaking=lines('persona-speaking',persona.data.core.speaking_style),interests=lines('persona-interests',persona.data.interests);
  if([speaking,interests].some(items=>items.length>100||items.some(value=>value.length>16000))){element('profile-notice').textContent='说话方式和兴趣各最多100项，每项最多16000字。';return;}
  const before=persona,token=profileEpoch,changes={name:element('persona-name').value,core:{...persona.data.core,description:element('persona-description').value,speaking_style:speaking},interests};
  profileBusy=true;profileControls();
  try{
   const result=await call('config.patch',{kind:'persona',expected:before.version,changes});
   if(token!==profileEpoch)return;
   persona={...before,version:result.version,data:{...before.data,...changes}};fillProfile();
   element('profile-notice').textContent='资料已保存。';
  }catch(error){if(token===profileEpoch)element('profile-notice').textContent=error.message;}
  finally{if(token===profileEpoch){profileBusy=false;profileControls();}}
 };
 element('unlock').onclick=()=>perform(async()=>{
  const password=element('unlock-password').value;element('unlock-password').value='';
  identity=await call('session.unlock',{password});render();await loadProfile();await loadBackups();await loadRuntime();notice('已解锁。');
 });
 element('enable-lock').onclick=()=>perform(async()=>{
  const password=element('lock-password').value;element('lock-password').value='';
  identity=await call('lock.enable',{password});render();notice('已启用应用锁，下次打开栖伴需要解锁。');
 });
 element('lock-now').onclick=()=>perform(async()=>{
  if(locking)return;
  accessEpoch++;locking=true;identity=null;render();
  try{identity=await call('session.lock');notice('管理和会客室已锁定。');}
  finally{locking=false;render();if(!identity&&ready)void refresh();}
 });
 element('disable-lock').onclick=()=>perform(async()=>{
  const password=element('lock-password').value;element('lock-password').value='';
  identity=await call('lock.disable',{password});render();notice('应用锁已关闭，之后按 Windows 登录身份直接管理。');
 });
 element('recover-lock').onclick=async()=>{
  if(recovering)return;
  let submitted=false;
  try{
   const password=element('recovery-password').value,confirm=element('recovery-confirm').value;
   element('recovery-password').value='';element('recovery-confirm').value='';
   if(password.length<12||password.length>256)throw new Error('新应用密码需为 12 至 256 个字符。');
   if(password!==confirm)throw new Error('两次输入的新密码不一致，请重新填写。');
   recovering=true;render();submitted=true;
   recoveryNotice('请在 Windows 验证窗口中完成身份验证。');notice('');
   identity=await call('',{password},'recover-lock');render();
   const message='应用密码已重设，资料已保留。下次打开请使用新密码解锁。';
   recoveryNotice(message);notice(message);feedback('应用密码已重设',message);
  }
  catch(error){recoveryNotice(error.message);notice(error.message);feedback(submitted?'密码重设提示':'请检查新密码',error.message);}
  finally{recovering=false;render();}
  if(identity&&!identity.locked)await perform(async()=>{await loadProfile();await loadBackups();await loadRuntime();});
 };
 // A restore receipt names what was restored. A local backup restore keeps that source in
 // `name`; a portable or preserved restore records the source it restored in `source_name`
 // and, separately, the copy of this machine's own data taken just before the switch in
 // `preservation_name`. That preservation copy is never the source: a receipt with no
 // recorded source (including an older portable receipt that kept only the preservation copy
 // in `name`) is reported as an unknown source rather than by reusing that copy's name.
 function restoreSource(receipt){
  if(typeof receipt.source_name==='string'&&receipt.source_name)return receipt.source_name;
  return receipt.source===undefined&&typeof receipt.name==='string'?receipt.name:'';
 }
 function restorePreservation(receipt){
  if(typeof receipt.preservation_name==='string'&&receipt.preservation_name)return receipt.preservation_name;
  return receipt.source!==undefined&&typeof receipt.name==='string'?receipt.name:'';
 }
 async function loadBackups(){
  const epoch=accessEpoch,result=await call('backups.list');
  if(epoch!==accessEpoch||!ready||!identity||identity.locked)return;
  const selection=element('backup-selection'),selected=selection.value;
  selection.replaceChildren();
  for(const backup of result.backups){const option=document.createElement('option');option.value=backup.name;option.textContent=`${backup.name} · ${(backup.bytes/1024).toFixed(1)} KB`;selection.append(option);}
  if(result.backups.some(item=>item.name===selected))selection.value=selected;
  if(result.last_restore){
   const receipt=result.last_restore,source=restoreSource(receipt),preservation=restorePreservation(receipt);
   element('backup-notice').textContent=receipt.status==='complete'
    ?`上次恢复已完成：${source||'来源未标注'}${preservation?`；恢复前资料已保全为 ${preservation}`:''}。角色保持停止。`
    :'资料已恢复，正在补齐登记图片；完成前不能运行角色。';
  }
  render();
 }
 async function backupWork(work){
  if(backupBusy)return;backupBusy=true;render();
  try{await work();}catch(error){element('backup-notice').textContent=error.message;feedback('备份与恢复提示',error.message);}
  finally{backupBusy=false;render();}
 }
 element('backup-create').onclick=()=>backupWork(async()=>{
  backupPreview=null;element('backup-effect').textContent='';element('backup-notice').textContent='正在保存备份……';
  const result=await call('backups.create');await loadBackups();element('backup-selection').value=result.name;
  element('backup-notice').textContent='加密备份已保存。';
 });
 element('backup-refresh').onclick=()=>backupWork(loadBackups);
 element('backup-selection').onchange=()=>{backupPreview=null;element('backup-effect').textContent='';render();};
 element('backup-preview').onclick=()=>backupWork(async()=>{
  backupPreview=null;
  const name=element('backup-selection').value;if(!name)throw new Error('请先保存或选择一份备份。');
  backupPreview=await call('backups.preview',{name});
  element('backup-effect').textContent=`${backupPreview.name}：包含 ${backupPreview.records} 条当前及历史资料。${backupPreview.effect}`;
  element('backup-notice').textContent='请检查所选备份。确认覆盖后将弹出 Windows 身份验证。';
 });
 element('backup-restore').onclick=()=>backupWork(async()=>{
  if(!backupPreview)return;
  const token=backupPreview.token;backupPreview=null;
  element('backup-notice').textContent='请在 Windows 窗口中验证身份，确认后开始覆盖恢复。';
  const result=await call('',{token},'restore-backup');
  const message=`已从 ${restoreSource(result.last_restore)||'所选备份'} 恢复资料，当前应用密码锁保持不变。角色保持停止，请重新检查平台授权。`;
  element('backup-notice').textContent=message;feedback('资料恢复完成',message);
  identity=await call('session.status');render();await loadProfile(true);await loadBackups();
 });
 // ------------------------------------------------------------------ portable
 // Portable cross-machine transfer. This page never sends a path and never sends a
 // switch: the window owns the pickers, and the only switch request it makes follows a
 // verified Windows prompt. Passwords are read here, cleared immediately and are never
 // written to a notice, the source summary or the feedback dialog.
 const activeBackupSource=()=>document.body?.dataset.tabBackup??portableKind;
 function renderPortableSource(){
  const matches=activeBackupSource()===portableKind;
  element('portable-preview-view').hidden=!matches;
  element('portable-empty').hidden=matches&&(!!portablePreview||!!element('portable-notice').textContent);
 }
 const portableSay=text=>{
  element('portable-notice').textContent=text;
  renderPortableSource();
 };
 window.addEventListener('qiban-tab-change',({detail})=>{
  if(detail.group==='backup')renderPortableSource();
 });
 function clearPortable(){
  portableEpoch++;portablePreview=null;portableBusy=false;portableKind='portable';
  preservedItems=[];preservedCursor='';preservedSelection='';
  element('portable-password').value='';element('portable-confirm').value='';
  element('portable-source').textContent='';
  element('preserved-selection').replaceChildren();
 }
 function portablePassword(requireConfirm){
  const password=element('portable-password').value,confirm=element('portable-confirm').value;
  element('portable-password').value='';element('portable-confirm').value='';
  if(password.length<16||password.length>256)throw new Error('独立恢复口令需为 16 至 256 个字符。');
  if(/\p{Cc}/u.test(password))throw new Error('独立恢复口令不能包含控制字符。');
  if(requireConfirm&&password!==confirm)throw new Error('两次输入的独立恢复口令不一致，请重新填写。');
  return password;
 }
 function portableSize(bytes){
  const value=Number(bytes);
  if(!Number.isFinite(value)||value<=0)return '大小未知';
  return value>=1048576?`${(value/1048576).toFixed(1)} MB`:`${Math.max(1,Math.round(value/1024))} KB`;
 }
 // Only what the service reports for a person: the two record counts, the source labels
 // and the preservation backup. `counters` counts the encrypted store and stays hidden.
 // A retained local copy has no source machine: it shows its own file name and file time.
 function portableTime(value){
  const text=String(value||'').replace('T',' ');
  return /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(text)?text.slice(0,19):'';
 }
 function describePortable(preview){
  const source=preview.source||{},preservation=preview.preservation||{};
  if(preview.kind==='preserved'){
   const parts=[`保全文件：${preview.name||'未标注'}`];
   const when=portableTime(preview.taken_at);
   if(when)parts.push(`文件时间：${when}`);
   parts.push(`包含 ${Number(preview.records)||0} 条当前资料、${Number(preview.history)||0} 条历史`,
    `接续角色：${source.role||'未标注'}`,`项目：${source.project||'未标注'}`,
    '恢复方式：完整替换当前资料，不与当前资料合并',
    '保留：当前 Windows 管理员绑定与应用密码锁、本机平台登录；恢复后全部平台保持停止');
   if(preservation.name)parts.push(`恢复前会先把当前资料另存为 ${preservation.name}`);
   if(typeof preview.effect==='string'&&preview.effect)parts.push(preview.effect);
   return parts.join(' · ');
  }
  const parts=[`来源角色：${source.role||'未标注'}`,`来源项目：${source.project||'未标注'}`,
   `来源安装：${source.installation||'未标注'}`,
   `包含 ${Number(preview.records)||0} 条当前资料、${Number(preview.history)||0} 条历史`,
   '恢复方式：完整替换当前资料，不与当前资料合并',
   '保留：当前 Windows 管理员绑定与应用密码锁、本机平台登录；恢复后全部平台保持停止'];
  if(preservation.name)parts.push(`恢复前已保全为 ${preservation.name}`);
  if(typeof preview.effect==='string'&&preview.effect)parts.push(preview.effect);
  return parts.join(' · ');
 }
 async function portableWork(work){
  if(portableBusy)return;
  const epoch=portableEpoch;portableBusy=true;render();
  try{await work(epoch);}
  catch(error){
   // A late failure from a session that was locked or disconnected meanwhile is dropped.
   if(epoch!==portableEpoch)return;
   portableSay(error.message);feedback('便携备份提示',error.message);
  }
  finally{if(epoch===portableEpoch){portableBusy=false;render();}}
 }
 element('portable-export').onclick=()=>portableWork(async epoch=>{
  const password=portablePassword(true);
  portablePreview=null;portableKind='portable';element('portable-source').textContent='';portableSay('正在导出便携备份，请稍候……');
  const result=await call('',{password},'portable-export');
  if(epoch!==portableEpoch)return;
  const message=`已导出备份 ${result.backup}（${portableSize(result.backup_bytes)}）与独立恢复密钥 ${result.key_file}（${portableSize(result.key_bytes)}），包含 ${result.records} 条当前资料、${result.history} 条历史。请把这一对文件放在一起保管：缺少任一文件或忘记独立恢复口令，都无法在另一台电脑恢复。`;
  portablePreview=null;render();portableSay(message);feedback('便携备份已导出',message);
 });
 element('portable-import').onclick=()=>portableWork(async epoch=>{
  const password=portablePassword(false);
  portablePreview=null;portableKind='portable';element('portable-source').textContent='';render();
  portableSay('先选择便携备份（.ccbackup），再选择同名密钥（.cckey）以查看预览。');
  const preview=await call('',{password},'portable-preview');
  if(epoch!==portableEpoch)return;
  portablePreview=preview;portableKind='portable';element('portable-source').textContent=describePortable(preview);
  portableSay('已读取来源预览。确认完整替换后会弹出 Windows 身份验证；取消验证不会改变当前资料。');
  render();
 });
 // This machine's retained copies. They need no recovery password and no file picker: the
 // page only names an opaque handle the backend issued for a copy it already enumerated,
 // and the same Windows confirmation performs the replacement.
 function preservedLabel(item){
  const when=portableTime(item.taken_at);
  return `${when?when+' · ':''}${item.name||'保全资料'} · ${portableSize(item.bytes)}${item.usable===false?' · 无法读取':''}`;
 }
 function appendPreserved(result){
  let first=null;
  for(const item of (result.items||[])){
   if(!item||!item.handle)continue;
   const option=document.createElement('option');
   option.value=item.handle;option.textContent=preservedLabel(item);
   if(item.usable===false)option.disabled=true;else if(!first)first=item.handle;
   element('preserved-selection').append(option);preservedItems.push(item);
  }
  preservedCursor=result.next||'';
  if(!preservedSelection&&first)preservedSelection=first;
  element('preserved-selection').value=preservedSelection;
 }
 // Choosing another copy invalidates any armed preview: the confirmation may only ever apply
 // to the copy whose preview the user just read, so the button goes back to disabled until
 // that copy is previewed again.
 element('preserved-selection').onchange=()=>{
  const chosen=element('preserved-selection').value||'';
  if(chosen===preservedSelection){render();return;}
  preservedSelection=chosen;
 if(portablePreview&&portableKind==='preserved'){
   portablePreview=null;portableKind='preserved';
   element('portable-source').textContent='';
   portableSay('已更改选择，请重新查看这份保全资料的预览后再确认。');
  }
  render();
 };
 element('preserved-list').onclick=()=>portableWork(async epoch=>{
  preservedItems=[];preservedCursor='';preservedSelection='';
  element('preserved-selection').replaceChildren();
  portablePreview=null;portableKind='preserved';element('portable-source').textContent='';render();
  portableSay('正在读取本机保全资料……');
  const result=await call('',{after:''},'portable-preserved-list');
  if(epoch!==portableEpoch)return;
  appendPreserved(result);render();
  const total=preservedItems.filter(item=>item.usable!==false).length;
  portableSay(total?`已读取 ${total} 份本机保全资料。选择一份后查看预览；恢复会先另存当前资料，并在 Windows 验证后完整替换。`:(preservedCursor?'本页没有可读取的保全资料，可以继续读取。':'当前没有可读取的本机保全资料。'));
 });
 element('preserved-more').onclick=()=>portableWork(async epoch=>{
  const after=preservedCursor;
  if(!after)return;
  portableSay('正在继续读取本机保全资料……');
  const result=await call('',{after},'portable-preserved-list');
  if(epoch!==portableEpoch)return;
  appendPreserved(result);render();
  portableSay(preservedCursor?'已读取更多保全资料，可以继续读取或查看预览。':'已读取全部本机保全资料。');
 });
 element('preserved-preview').onclick=()=>portableWork(async epoch=>{
  const handle=preservedSelection||element('preserved-selection').value;
  if(!handle){portableSay('请先选择一份本机保全资料。');return;}
  portablePreview=null;portableKind='preserved';element('portable-source').textContent='';render();
  portableSay('正在验证这份本机保全资料……');
  const preview=await call('',{handle},'portable-preserved-preview');
  if(epoch!==portableEpoch)return;
  portablePreview=preview;portableKind='preserved';
  element('portable-source').textContent=describePortable(preview);
  portableSay('已读取保全资料预览。确认完整替换后会弹出 Windows 身份验证；取消验证不会改变当前资料。');
  render();
 });
 // The confirmation is the one action whose report must survive its own side effect: the
 // window stops the old backend on success, so a ready->false transition must not swallow
 // the outcome (or the no-retry rule for an unknown one).
 element('portable-restore').onclick=async()=>{
  if(portableBusy||recovering||!portablePreview||!ready||identity?.locked||activeBackupSource()!==portableKind)return;
  const token=portablePreview.token,preserved=portableKind==='preserved';
  portablePreview=null;element('portable-source').textContent='';
  portableBusy=true;render();
  portableSay('请在 Windows 窗口中验证身份。验证通过后才会完整替换当前资料；取消不会改变资料。');
  let result=null,failure=null;
  try{result=await call('',{token},'portable-restore');}
  catch(error){failure=error;}
  portableBusy=false;
  // The listing and its selection describe the data this attempt is replacing.
  preservedItems=[];preservedCursor='';preservedSelection='';
  element('preserved-selection').replaceChildren();render();
  const switched=!failure&&!!result&&!!result.switched;
  // "restart_required without switched" is the service's unknown outcome: never report it
  // as a failure and never invite a resubmission, only a restart and a check.
  const unknown=!failure&&!switched&&!!result&&!!result.restart_required;
  const preservation=result&&result.preservation&&result.preservation.name?result.preservation.name:'';
  const done=preserved?'已从本机保全资料恢复完成：当前资料已被这份保全资料完整替换，当前应用密码锁保持不变。'
   :'便携恢复已切换完成：当前资料已被来源资料完整替换，当前应用密码锁保持不变。';
  const message=failure?failure.message:switched
   ?`${done}${preservation?`恢复前的资料已保全为 ${preservation}。`:''}后台已停止，请点击上方『启动应用后台』重新启动后台后检查会客室与角色；角色保持停止。`
   :unknown?`${result.message||'切换步骤可能已提交，结果未知。'}恢复前的资料已保全${preservation?`为 ${preservation}`:''}；已停止后台且不会重复提交，请点击『启动应用后台』重新启动后台后核对资料。`
   :(result&&result.message?result.message:'后台未完成切换，当前资料未改变；请重新预览后再试。');
  portableKind=preserved?'preserved':'portable';portableSay(message);
  feedback(failure||!switched&&!unknown?(preserved?'本机保全资料恢复未完成':'便携恢复未完成')
   :switched?(preserved?'本机保全资料恢复完成':'便携恢复完成'):'便携恢复结果未知',message);
 };
 element('portable-cancel').onclick=()=>portableWork(async epoch=>{
  if(!portablePreview||activeBackupSource()!==portableKind)return;
  portablePreview=null;element('portable-source').textContent='';render();
  portableSay('正在取消本次导入……');
  const result=await call('',{},'portable-cancel');
  if(epoch!==portableEpoch)return;
  const message=result&&result.cancelled?'已取消本次导入，当前资料未改变。需要恢复时请重新预览。':'已放弃本次导入，未执行切换。';
  portableSay(message);feedback('已取消本次导入',message);
 });
 const runtimePending=new Set();
 async function runtimeAction(operation,args){
  const key=args?.platform,epoch=accessEpoch;
  if(key&&runtimePending.has(key))return;
  if(key)runtimePending.add(key);
  try{await call(operation,args);}
  catch(error){if(epoch===accessEpoch)feedback('运行设置提示',error.message);}
  finally{
   if(key)runtimePending.delete(key);lastRender='';observationRevision++;
   if(runtimeRefreshing)refreshAfterAction=true;else await perform(loadRuntime);
  }
 }
 function paintMonitor(state,frozen=false){
  const scene=element('monitor-scene');scene.dataset.state=state||'idle';
  scene.dataset.moving=String(!frozen&&!observationPaused&&['learning','replying','browsing','watching'].includes(state));
  element('monitor-window').setActivity?.(state,frozen||observationPaused);
 }
 function resetInbox(){
  inboxSequence++;inboxPage=inboxPages=1;inboxSnapshot='';inboxItems=[];inboxSelected.clear();inboxBusy=confirmDelete=false;
  element('inbox-tools').hidden=element('inbox-pages').hidden=true;
  element('inbox-feedback').textContent='';element('inbox-jump').value='';
 }
 function inboxControls(){
  const count=inboxSelected.size,select=element('inbox-select');
  for(const row of element('notification-items').children){
   if(row.className?.startsWith('inbox-row'))row.children[0].disabled=inboxBusy;
  }
  select.checked=count>0&&count===inboxItems.length;select.indeterminate=count>0&&!select.checked;
  select.disabled=inboxBusy; element('inbox-read').disabled=inboxBusy||!inboxItems.some(i=>!i.read&&inboxSelected.has(i.id));
  const remove=element('inbox-delete');remove.disabled=inboxBusy||!count;
  remove.textContent=confirmDelete?`确认删除 ${count} 条`:'删除所选';
  element('inbox-cancel').hidden=!confirmDelete;element('inbox-cancel').disabled=inboxBusy;
  element('inbox-prev').disabled=inboxBusy||inboxPage<=1;element('inbox-next').disabled=inboxBusy||inboxPage>=inboxPages;
  element('inbox-go').disabled=inboxBusy;
 }
 function paintInbox(data){
  const signature=JSON.stringify(data);
  if(signature===inboxSnapshot)return;
  if(inboxPage!==data.page){inboxSelected.clear();confirmDelete=false;}
  inboxSnapshot=signature;inboxPage=data.page;inboxPages=data.pages;inboxItems=data.items;
  const selected=new Set(inboxItems.filter(i=>inboxSelected.has(i.id)).map(i=>i.id));
  if(selected.size!==inboxSelected.size)confirmDelete=false;
  inboxSelected=selected;
  element('notification-title').textContent=`应用通知 · ${data.unread} 条未读`;
  element('inbox-tools').hidden=!data.total;element('inbox-pages').hidden=data.pages<=1;
  element('inbox-position').textContent=`第 ${data.page} / ${data.pages} 页 · 共 ${data.total} 条`;
  element('inbox-jump').max=data.pages;
  const list=element('notification-items');list.replaceChildren();
  if(!inboxItems.length){const empty=document.createElement('p');empty.textContent='暂无应用通知。';list.append(empty);}
  const names={qq:'QQ',wechat:'微信',bilibili:'B站',local:'本机'};
  for(const item of inboxItems){
   const row=document.createElement('label'),check=document.createElement('input'),message=document.createElement('span'),detail=document.createElement('small');
   check.type='checkbox';check.checked=selected.has(item.id);check.disabled=inboxBusy;
   check.onchange=()=>{if(check.checked)inboxSelected.add(item.id);else inboxSelected.delete(item.id);confirmDelete=false;inboxControls();};
   message.textContent=`${names[item.platform]||'栖伴'} · ${item.message}${item.occurrences>1?'（累计 '+item.occurrences+' 次）':''}`;
   detail.textContent=(item.read?'已读':'未读')+' · '+new Date(item.created_at).toLocaleString();
   row.className=item.read?'inbox-row':'inbox-row unread';row.append(check,message,detail);list.append(row);
  }
  inboxControls();
 }
 async function loadInbox(page){
  const seq=++inboxSequence,epoch=accessEpoch;
  try{
   const data=await call('notifications.page',{page});
   if(seq===inboxSequence&&epoch===accessEpoch&&ready&&identity&&!identity.locked)paintInbox(data);
  }catch(error){if(seq===inboxSequence)element('inbox-feedback').textContent=error.message;}
 }
 async function goInbox(page){
  if(inboxBusy||!Number.isInteger(page)||page<1||page>inboxPages){
   if(!inboxBusy)element('inbox-feedback').textContent=`请输入 1 至 ${inboxPages} 的页码。`;
   return;
  }
  if(page===inboxPage)return;
  inboxBusy=true;inboxControls();
  const epoch=accessEpoch;confirmDelete=false;element('inbox-feedback').textContent='';
  try{await loadInbox(page);}finally{if(epoch===accessEpoch){inboxBusy=false;inboxControls();}}
 }
 async function updateInbox(action){
  if(inboxBusy||!inboxSelected.size)return;
  if(action==='delete'&&!confirmDelete){confirmDelete=true;inboxControls();return;}
  const epoch=accessEpoch,identifiers=[...inboxSelected];
  inboxBusy=true;confirmDelete=false;inboxSequence++;inboxControls();
  try{
   const result=await call('notifications.update',{action,identifiers});
   if(epoch!==accessEpoch)return;
   inboxSelected.clear();
   for(const row of element('notification-items').children){if(row.className?.startsWith('inbox-row'))row.children[0].checked=false;}
   element('inbox-feedback').textContent=action==='delete'?`已删除 ${result.changed} 条通知。`:`已将 ${result.changed} 条通知标为已读。`;
  }catch(error){if(epoch===accessEpoch)element('inbox-feedback').textContent=error.message;}
  finally{if(epoch===accessEpoch){inboxBusy=false;inboxSnapshot='';await loadInbox(inboxPage);inboxControls();}}
 }
 element('inbox-select').onchange=()=>{
  const checked=element('inbox-select').checked;
  inboxSelected=new Set(checked?inboxItems.map(i=>i.id):[]);confirmDelete=false;
  for(const row of element('notification-items').children)row.children[0].checked=checked;
  inboxControls();
 };
 element('inbox-read').onclick=()=>updateInbox('read');element('inbox-delete').onclick=()=>updateInbox('delete');
 element('inbox-cancel').onclick=()=>{confirmDelete=false;inboxControls();};
 element('inbox-prev').onclick=()=>goInbox(inboxPage-1);element('inbox-next').onclick=()=>goInbox(inboxPage+1);
 element('inbox-go').onclick=()=>goInbox(Number(element('inbox-jump').value));
 element('inbox-jump').onkeydown=event=>{if(event.key==='Enter')element('inbox-go').onclick();};
 async function loadRuntime(){
  if(runtimeRefreshing||!ready||!identity||identity.locked)return;
  runtimeRefreshing=true;const runtimeEpoch=accessEpoch,revision=observationRevision;let snapshotApplied=false;
  element('runtime-refresh').disabled=true;
  try{
   const state=await call('runtime.status');
   if(!ready||!identity||identity.locked||runtimeEpoch!==accessEpoch||revision!==observationRevision)return;
   const activities=state.activities||[],active=activities[0];
   const fault=state.platforms.some(p=>p.scheduler_error||p.failed)||state.handoff_pending;
   const activityLabels={replying:'正在撰写回复',learning:'正在学习',watching:'正在观看视频',browsing:'正在浏览内容'};
   paintMonitor(active?.state||'idle');
   element('monitor-state').textContent=observationPaused?'观察已暂停':active?activityLabels[active.state]||'正在处理':fault?'运行待处理':state.platforms.some(p=>p.status==='active')?'正在等待新消息':'休息中';
   const details=activities.map(a=>({qq:'QQ',wechat:'微信',bilibili:'B 站',local:'本机会客室'}[a.platform]||a.platform)).filter(Boolean);
   const generated=typeof state.generated_at==='string'?Date.parse(state.generated_at):NaN;
   if(Number.isFinite(generated))details.push('快照时间 '+new Date(generated).toLocaleString('zh-CN',{hour12:false}));
   element('monitor-detail').textContent=details.join(' / ');
   window.QibanHouse?.snapshot(state);
   const signature=JSON.stringify([state.platforms,activities.map(a=>[a.platform,a.state]),state.handoff_pending,state.intake_paused,state.tasks_blocked,state.preference_revision,[...runtimePending]]);
   if(signature!==lastRender){
   const platforms=element('runtime-platforms');
   const expanded=new Set(Array.from(platforms.children).filter(n=>n.open).map(n=>n.dataset.platform));
   const focus=document.activeElement,focusedRow=focus?.closest?.('#runtime-platforms details');
   const focusKey=focus?.dataset.control||'',focusId=focusedRow?.dataset.platform;
   platforms.replaceChildren();
   const names={qq:'QQ',wechat:'微信',bilibili:'B站',local:'本机会客室'};
   for(const item of state.platforms){
    const row=document.createElement('details'),summary=document.createElement('summary'),actions=document.createElement('div');
    const title=document.createElement('h3'),status=document.createElement('p');
    summary.className='runtime-summary';actions.className='runtime-actions';
    row.dataset.platform=item.platform;row.open=expanded.has(item.platform);summary.dataset.control='summary';
    title.textContent=names[item.platform]||item.platform;
    status.textContent=item.scheduler_error?'后台任务调度已停止，请重启应用后台':state.handoff_pending?'账号交接未完成，任务保持停止':state.tasks_blocked?'后台任务已停止':item.stopping?'正在停止，请稍候':!item.available?'连接器尚未接入':item.status==='active'&&state.intake_paused&&['qq','wechat','bilibili'].includes(item.platform)?'历史容量已满，暂停接收新消息':item.status==='active'?(item.busy?'正在处理任务':'运行中'):item.failed?(item.message||'运行中断，请查看记录'):item.held?'已停止':!item.model_ready?'等待模型就绪':'已停止';
    const label=document.createElement('label'),toggle=document.createElement('input');toggle.type='checkbox';toggle.checked=item.autorun;
    toggle.dataset.control='preference';toggle.disabled=runtimePending.has(item.platform)||(!item.available&&!item.autorun);
    toggle.onchange=()=>runtimeAction('runtime.preference',{platform:item.platform,enabled:toggle.checked,expected:state.preference_revision});
    label.append(toggle,document.createTextNode('下次打开时自动运行'));
    label.title='下次打开栖伴时，条件就绪便会启动这个平台。';
    const start=document.createElement('button'),stop=document.createElement('button');start.textContent='启动';stop.textContent='停止本次运行';stop.className='secondary';
    start.disabled=state.tasks_blocked||item.stopping||!!item.scheduler_error||!item.available||!item.model_ready||item.status==='active';stop.disabled=item.stopping||(item.status!=='active'&&(!item.autorun||item.held));
    start.dataset.control='start';stop.dataset.control='stop';
    start.onclick=()=>{start.disabled=true;start.textContent='正在启动…';status.textContent='正在启动…';return runtimeAction('runtime.control',{platform:item.platform,action:'start'});};
    stop.onclick=()=>{stop.disabled=true;status.textContent='正在停止…';return runtimeAction('runtime.control',{platform:item.platform,action:'stop'});};
    if(runtimePending.has(item.platform)){start.disabled=stop.disabled=true;start.textContent='处理中…';status.textContent='正在更新运行状态…';}
    const badge=document.createElement('span');badge.className='house-badge';
    const issue=item.failed||item.scheduler_error||state.handoff_pending;
    badge.dataset.tone=issue?'issue':item.status==='active'?'active':'quiet';
    badge.textContent=issue?'需处理':item.stopping?'停止中':!item.available?'未接入':item.status==='active'?(item.read_only?'只读运行':'运行中'):item.status==='paused'||item.held?'已停止':!item.model_ready?'待就绪':'已停止';
    summary.append(title,status,badge);actions.append(start);
    if(item.platform==='bilibili'){
     const readOnly=document.createElement('button');readOnly.textContent='只读启动';readOnly.className='secondary';
     readOnly.dataset.control='read-only';
     readOnly.disabled=start.disabled;readOnly.onclick=()=>{start.disabled=readOnly.disabled=true;readOnly.textContent='正在启动…';status.textContent='正在启动…';return runtimeAction('runtime.control',{platform:item.platform,action:'start_read_only'});};
     if(item.status==='active'&&item.read_only)status.textContent+='（只读）';
     actions.append(readOnly);
    }
    const currentActivities=activities.filter(a=>a.platform===item.platform).map(a=>activityLabels[a.state]||'正在处理');
    if(!runtimePending.has(item.platform)&&currentActivities.length)status.textContent=currentActivities.join(' · ');
    actions.append(stop);row.append(summary,actions,label);platforms.append(row);
    if(focusId===item.platform&&focusKey)Array.from(row.querySelectorAll('[data-control]')).find(n=>n.dataset.control===focusKey)?.focus({preventScroll:true});
   }
   if(!state.platforms.length){const empty=document.createElement('p');empty.textContent='当前没有可查看的平台。';platforms.append(empty);}
   lastRender=signature;
   }
   snapshotApplied=true;
   if(!inboxBusy)await loadInbox(inboxPage);
   lastRuntimeRefresh=Date.now();element('runtime-refresh').textContent='立即刷新';
  }catch(error){
   if(!ready||!identity||identity.locked||runtimeEpoch!==accessEpoch||revision!==observationRevision)return;
   if(!snapshotApplied){
    element('monitor-state').textContent='监控连接中断';
    paintMonitor(element('monitor-scene').dataset.state,true);
    window.QibanHouse?.disconnected();
   }
   element('runtime-refresh').hidden=false;element('runtime-refresh').textContent='重试';throw error;
  }finally{
   runtimeRefreshing=false;element('runtime-refresh').disabled=!ready||!identity||identity.locked;
   if(refreshAfterAction){refreshAfterAction=false;void perform(loadRuntime);}
  }
 }
 element('runtime-refresh').onclick=()=>perform(loadRuntime);
 element('runtime-observe').onclick=()=>{
  if(!ready||!identity||identity.locked)return;
  observationPaused=!observationPaused;observationRevision++;
  element('runtime-observe').textContent=observationPaused?'继续观察':'暂停观察';
  window.QibanHouse?.pause(observationPaused);
  if(observationPaused){paintMonitor(element('monitor-scene').dataset.state,true);element('monitor-state').textContent='观察已暂停';}
  else{lastRuntimeRefresh=0;if(runtimeRefreshing)refreshAfterAction=true;else void perform(loadRuntime);}
 };
 element('runtime-stop-all').onclick=()=>perform(async()=>{
  await call('runtime.stop_all',{});await loadRuntime();
  feedback('已请求停止全部','平台运行和手动任务均已停止接收新工作，正在退出的操作会完成清理。自动运行偏好保持不变。');
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='service'&&data.management){
   element('management-panel').hidden=false;windowsUser=data.windowsUser||windowsUser;
   const changed=ready!==data.ready;ready=data.ready;if(!ready){accessEpoch++;identity=null;}render();if(changed&&ready)void refresh();
   if(ready&&!changed&&identity&&!identity.locked&&!observationPaused&&!document.hidden&&Date.now()-lastRuntimeRefresh>2000)void loadRuntime().catch(error=>notice(error.message));
  }
  if(data.type==='management'){
   const job=pending.get(data.request_id);if(!job)return;clearTimeout(job.timer);pending.delete(data.request_id);
   if(data.response.ok)job.resolve(data.response.result);else job.reject(new Error(data.response.message||'管理操作未完成。'));
  }
  if(data.type==='management_error'){
   for(const job of pending.values()){clearTimeout(job.timer);job.reject(new Error(data.message));}pending.clear();
   accessEpoch++;identity=null;ready=false;render();notice(data.message||'管理连接已中断。');
  }
 });
 ['reload-persona','backup-refresh'].forEach(id=>window.QibanAutoRead?.register(id));
})();
