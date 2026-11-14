'use strict';
const send=action=>window.chrome.webview.postMessage({action});
for(const action of ['start','stop','pick','tray','exit']) document.getElementById(action).onclick=()=>send(action);
window.chrome.webview.addEventListener('message',({data})=>{
 if(data.type==='service') {
  document.getElementById('component-title').textContent=data.component==='postgresql'?'本地数据库':'应用后台';
  const label=data.component==='postgresql'?'数据库':'应用后台';
  const labels={stopped:`${label}已停止 · 页面仍可使用`,starting:`正在启动${label}……`,ready:`${label}已连接`,stopping:`正在停止${label}……`,failed:`${label}出现问题 · 页面仍可使用`};
  document.getElementById('start').textContent=`启动${label}`;
  document.getElementById('stop').textContent=`停止${label}`;
  document.getElementById('service').textContent=labels[data.state];
  document.getElementById('home-service').textContent=labels[data.state];
  const list=document.getElementById('components');list.replaceChildren();
  if(data.members.length>1)for(const member of data.members){const item=document.createElement('li');const name={'installation':'本机资料准备','postgresql':'数据库','upgrade-confirm':'升级校验','encrypted-storage':'加密资料后台'}[member.id]||'应用组件';const state={stopped:'已停止',starting:'启动中',ready:'已就绪',stopping:'停止中',failed:'失败'}[member.state];item.textContent=`${name}：${state}`;list.appendChild(item);}
  document.body.dataset.service=data.ready?'ready':'offline';
  document.body.dataset.component=data.state;
  document.getElementById('start').disabled=['starting','ready','stopping'].includes(data.state);
  document.getElementById('stop').disabled=['stopped','stopping'].includes(data.state);
  const notices={installation_directory_permissions:'无法设置本机资料目录权限，请检查目录访问权限后重试。',installation_busy:'本机资料正在准备，请稍后再试。',installation_failed:'本机资料准备失败，可重试并查看诊断。',installation_unknown_directory:'资料目录包含未识别内容，已停止准备。',installation_cluster_missing:'数据库完成记录不一致，已停止准备。',database_bundle_invalid:'数据库资源校验失败，请修复应用资源。',storage_runtime_missing:'存储组件文件缺失，请修复应用资源。',storage_bundle_invalid:'存储组件校验失败，请修复应用资源。',storage_vault_missing:'本次安装的凭据缺失，未打开资料。',storage_vault_invalid:'本次安装的凭据无效，未打开资料。',storage_integrity:'资料解密校验失败，已停止启动。',forced_stop:'后台未及时退出，已结束其进程。',stop_failed:'后台未正常退出，请检查本地诊断记录。',database_version_mismatch:'数据库资料与当前组件版本不匹配，未启动。',database_spec_invalid:'数据库配置无法读取，未启动。',database_runtime_missing:'数据库组件不完整，未启动。',database_owner_mismatch:'数据库资料不属于当前应用实例，未启动。',upgrade_maintenance_in_progress:'上次升级的准备工作没有做完，暂未打开资料；请重新运行这次升级所用的同一个安装包把它做完。',upgrade_pending:'上次升级还停在准备阶段，未打开资料；请重新运行这次升级所用的同一个安装包。',upgrade_confirmation_required:'新版本还没有完成这次升级的校验，暂不启动任务和平台；请重新启动栖伴完成校验。',upgrade_identity_unknown:'无法确认当前程序是不是这次升级的目标版本，未打开资料；请用开始菜单里的“栖伴升级回退”处理。',upgrade_previous_program_blocked:'资料已经交给新版本校验，旧版本不能继续写入；请用开始菜单里的“栖伴升级回退”回到升级前的版本。',upgrade_rollback_pending:'正在把资料切回升级前的版本，暂未打开资料；请先用开始菜单里的“栖伴升级回退”完成回退。',upgrade_recovery_pending:'这台电脑正在等待回退上一次升级，未打开资料；请先用开始菜单里的“栖伴升级回退”完成或放弃回退。',upgrade_confirm_program:'当前程序不是这次升级记录里的目标版本，未完成校验；请用开始菜单里的“栖伴升级回退”处理。',upgrade_startup_failed:'升级校验没有完成，未打开业务；请用开始菜单里的“栖伴升级回退”回到升级前的版本。',upgrade_startup_timeout:'升级校验等待超时，未打开业务；请用开始菜单里的“栖伴升级回退”检查升级状态。',upgrade_program_changed:'程序副本与升级记录不符，未打开资料；请保留资料目录并人工检查。',upgrade_journal_invalid:'升级记录无法读取，未打开资料；请保留资料目录并人工检查（可用开始菜单里的“栖伴升级回退”查看）。',upgrade_journal_missing:'没有找到这次升级的记录，未继续维护；请重新运行这次升级所用的同一个安装包。',upgrade_state:'升级记录处于无法识别的状态，未打开资料；请用开始菜单里的“栖伴升级回退”检查升级状态。'};
  document.getElementById('notice').textContent=notices[data.detail]||'';
 }
 if(data.type==='file') document.getElementById('file').textContent=data.name?'已选择：'+data.name:'已取消选择，未读取文件。';
 if(data.type==='notice')document.getElementById('notice').textContent=data.text;
});
send('status');
