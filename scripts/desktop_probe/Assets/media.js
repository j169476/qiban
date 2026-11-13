(()=>{
 'use strict';
 const el=id=>document.getElementById(id),pending=new Map();
 let allowed=false,busy=false,epoch=0,settings=null,budget=null,capacity=null,voice=null,items=[],after='';
 const ranges={images:[0,12],audio:[0,12],files:[0,12],single_mib:[1,10],total_mib:[1,120],audio_seconds:[1,600]};
 function call(operation,args={}){
  const id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(id);reject(new Error('连接未及时返回，请重新读取。'));},12000);
   pending.set(id,{resolve,reject,timer});
   window.chrome.webview.postMessage({action:'management',operation:'media.'+operation,arguments:args,request_id:id});
  });
 }
 function render(){
  for(const id of ['media-days','media-save','media-refresh','media-next'])el(id).disabled=!allowed||busy||!settings;
  el('media-refresh').disabled=!allowed||busy;
  for(const key of [...Object.keys(ranges),'budget-save'])el('media-'+key).disabled=!allowed||busy||!budget;
  for(const id of ['media-capacity','media-capacity-save'])el(id).disabled=!allowed||busy||!capacity;
  for(const id of ['media-voice-enabled','media-voice-seconds','media-voice-save'])el(id).disabled=!allowed||busy||!voice;
  el('media-next').disabled=!allowed||busy||items.length!==50;
  el('media-items').replaceChildren();
  if(!allowed)return;
  if(!items.length){const p=document.createElement('p');p.textContent=settings?'当前没有已保存的聊天附件。':'附件尚未读取。';el('media-items').append(p);}
  for(const item of items){
   const row=document.createElement('div'),name=document.createElement('p'),button=document.createElement('button');
   const kinds={image:'图片',audio:'语音',file:'文件'};
   const status=item.status==='expired'?(item.cleanup_reason==='capacity'?'因空间不足已清理原件，聊天记录保留':'原件已到期清理，聊天记录保留'):item.preserve.length?'长期保留':item.processed_at===null?'等待任务完成，暂不计时':'按保留期限或空间需要自动清理';
   name.textContent=`${item.name||kinds[item.kind]} · ${kinds[item.kind]} · ${item.bytes} 字节 · ${status}`;
   const scope=document.createElement('p');scope.className='note';
   scope.textContent=`${item.scope.platform.toUpperCase()} 账号 ${item.scope.account_identity} · ${item.scope.kind==='group'?'群':'好友'} ${item.scope.target_id}`;
   button.className='secondary';button.textContent=item.preserve.includes('favorite')?'取消收藏':'收藏并长期保留';
   button.disabled=busy||item.status==='expired';button.onclick=()=>perform(async()=>{
    const reasons=item.preserve.includes('favorite')?item.preserve.filter(v=>v!=='favorite'):[...item.preserve,'favorite'];
    await call('preserve',{identifier:item.id,reasons,expected:item.revision});
    return reasons.includes('favorite')?'已收藏，附件原件将长期保留。':'已取消收藏；如无其他长期保留标记，将按当前期限清理。';
   });
   row.append(name,scope,button);el('media-items').append(row);
  }
 }
 async function load(token){
  const result=await call('list',{after,limit:50});
  if(!allowed||token!==epoch)return;
  settings=result.settings;items=result.items;el('media-days').value=String(settings.days);
  budget=result.budget;
  capacity=result.capacity;
  voice=result.voice;
  if(voice){el('media-voice-enabled').checked=voice.enabled;el('media-voice-seconds').value=String(voice.seconds);}
  if(capacity){
   el('media-capacity').value=String(capacity.mib);
   const mib=value=>(value/1048576).toFixed(2);
   el('media-capacity-usage').textContent=`已用 ${mib(capacity.used_bytes)} MiB，其中受保护 ${mib(capacity.protected_bytes)} MiB、可回收 ${mib(capacity.reclaimable_bytes)} MiB。`+(capacity.over_limit?' 当前超过所设容量，正在按规则回收；受保护内容不会删除。':'');
  }
  if(budget)for(const key of Object.keys(ranges))el('media-'+key).value=String(budget.limits[key]);
  if(result.maintenance_error)el('media-notice').textContent='附件自动清理未完成，已停止本轮清理。请重启应用后台后检查。';
 }
 async function perform(action,noticeId='media-notice'){
  if(!allowed||busy)return;busy=true;const token=epoch;render();
  try{
   const notice=action?await action():null;
   if(!allowed||token!==epoch)return;
   await load(token);
   if(allowed&&token===epoch&&notice)el(noticeId).textContent=notice;
  }catch(error){if(allowed&&token===epoch)el(noticeId).textContent=error.message;}
  finally{if(token===epoch){busy=false;render();}}
 }
 el('media-save').onclick=()=>{
  const days=Number(el('media-days').value);
  if(!Number.isInteger(days)||days<1||days>3650){el('media-notice').textContent='请输入 1 至 3650 的整数天数。';return;}
  return perform(async()=>{await call('configure',{days,expected:settings.revision});return `已保存：处理完成后保留 ${days} 天，收藏等长期保留素材不受影响。`;});
 };
 el('media-refresh').onclick=()=>{after='';return perform();};
 el('media-capacity-save').onclick=()=>{
  if(!allowed||busy||!capacity)return;
  const mib=Number(el('media-capacity').value);
  if(!Number.isInteger(mib)||mib<1||mib>1048576){el('media-capacity-notice').textContent='请输入 1 至 1048576 的整数 MiB。';return;}
  return perform(async()=>{await call('capacity.configure',{mib,expected:capacity.revision});return '附件总容量已保存；后台将按保护规则回收超出部分。';},'media-capacity-notice');
 };
 el('media-budget-save').onclick=()=>{
  if(!allowed||busy||!budget)return;
  const limits=Object.fromEntries(Object.keys(ranges).map(key=>[key,Number(el('media-'+key).value)]));
  if(Object.entries(ranges).some(([key,[low,high]])=>!el('media-'+key).value.trim()||!Number.isInteger(limits[key])||limits[key]<low||limits[key]>high)){
   el('media-budget-notice').textContent='请输入各项标注范围内的整数。';return;
  }
  return perform(async()=>{await call('budget.configure',{limits,expected:budget.revision});return '附件处理上限已保存，从下一次回复开始使用。';},'media-budget-notice');
 };
 el('media-voice-save').onclick=()=>{
  if(!allowed||busy||!voice)return;
  const seconds=Number(el('media-voice-seconds').value),enabled=el('media-voice-enabled').checked;
  if(!Number.isInteger(seconds)||seconds<1||seconds>600){el('media-voice-notice').textContent='请输入1至600秒的整数。';return;}
  return perform(async()=>{await call('voice.configure',{enabled,seconds,expected:voice.revision});return '语音回复设置已保存。';},'media-voice-notice');
 };
 el('media-next').onclick=()=>{after=items.at(-1)?.id||'';return perform();};
 window.addEventListener('qiban-management-state',({detail})=>{
  const next=detail.ready&&!detail.locked,changed=allowed!==next;allowed=next;
  el('media-panel').hidden=el('management-panel').hidden;
  if(!allowed){epoch++;busy=false;settings=null;budget=null;capacity=null;voice=null;el('media-voice-enabled').checked=false;el('media-voice-seconds').value='';el('media-voice-notice').textContent='';items=[];after='';el('media-days').value='';el('media-notice').textContent='';el('media-budget-notice').textContent='';for(const key of Object.keys(ranges))el('media-'+key).value='';for(const id of ['media-capacity','media-capacity-usage','media-capacity-notice']){el(id).value='';el(id).textContent='';}}
  render();if(allowed&&changed)void perform();
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management'){
   const job=pending.get(data.request_id);if(!job)return;clearTimeout(job.timer);pending.delete(data.request_id);
   if(data.response.ok)job.resolve(data.response.result);else job.reject(new Error(data.response.message||'附件操作未完成。'));
  }
  if(data.type==='management_error'){
   for(const job of pending.values()){clearTimeout(job.timer);job.reject(new Error(data.message));}pending.clear();
  }
 });
 render();
})();
