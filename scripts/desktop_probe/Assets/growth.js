(()=>{
 'use strict';
 const el=id=>document.getElementById(id),pending=new Map();
 let allowed=false,busy=false,epoch=0,proposal=null,next=null;
 const reasons={readable:'当前公开成长状态可正常读取，无需恢复。',public_growth_policy_missing:'缺少来源登记。',public_growth_source_changed:'来源版本已改变。',public_growth_source_denied:'来源权限已失效。',public_growth_invalid:'状态完整性检查未通过。',public_growth_validation_failed:'来源验证未完成。'};
 const names={interests:'兴趣',mood:'心情',reason:'原因',traits:'特点'};
 function call(operation,args={}){
  const id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(id);reject(new Error('未及时收到结果，请重新检查状态；不要重复提交恢复。'));},15000);
   pending.set(id,{resolve,reject,timer});
   window.chrome.webview.postMessage({action:'management',operation:'growth.'+operation,arguments:args,request_id:id});
  });
 }
 function controls(){
  const ready=allowed&&!busy;
  el('growth-preview').disabled=!ready;el('growth-confirm').disabled=!ready||!proposal;
  el('growth-rebuild').disabled=!ready||!proposal||!el('growth-confirm').checked;
  el('growth-cancel').disabled=!ready||!proposal;el('growth-archives').disabled=!ready;
  el('growth-archives-next').disabled=!ready||!next;
  for(const button of el('growth-archive-list').children)button.disabled=!ready;
 }
 function clearProposal(){proposal=null;el('growth-confirm').checked=false;el('growth-proposal').hidden=true;el('growth-data').replaceChildren();}
 function describe(value){
  if(value===null||value===undefined)return '无记录';
  if(typeof value==='object')return Object.entries(value).filter(([key])=>key!=='schema_version').map(([key,item])=>`${names[key]||key}：${describe(item)}`).join('\n')||'未设置';
  return String(value)||'未设置';
 }
 function show(container,value){const p=document.createElement('p');p.textContent=describe(value);container.replaceChildren();container.append(p);}
 async function run(work,target='growth-notice'){
  if(!allowed||busy)return;busy=true;const token=epoch;controls();
  try{const commit=await work();if(allowed&&epoch===token)commit?.();}
  catch(error){if(allowed&&epoch===token)el(target).textContent=error.message;}
  finally{if(epoch===token){busy=false;controls();}}
 }
 el('growth-preview').onclick=()=>run(async()=>{
  clearProposal();el('growth-notice').textContent='正在检查公开成长…';
  const result=await call('preview');
  return ()=>{
   el('growth-notice').textContent=reasons[result.reason]||'检查未完成，请查看后台诊断。';
   if(result.token){proposal=result;el('growth-proposal').hidden=false;show(el('growth-data'),result.data);el('growth-notice').textContent+=' 请核对恢复内容；预览在两分钟后失效。';}
  };
 });
 el('growth-confirm').onchange=controls;
 el('growth-cancel').onclick=()=>{if(!allowed||busy)return;clearProposal();el('growth-notice').textContent='已取消恢复，公开成长未改变。';controls();};
 el('growth-rebuild').onclick=()=>{
  if(!allowed||busy||!proposal||!el('growth-confirm').checked)return;
  const token=proposal.token;clearProposal();
  return run(async()=>{
   await call('rebuild',{token});
   return ()=>{el('growth-notice').textContent='公开成长已恢复，旧状态已加密归档。B 站保持停止，请核对后自行启动。';el('growth-archive-content').replaceChildren();el('growth-archive-list').replaceChildren();next=null;};
  });
 };
 function archives(after){return run(async()=>{
  el('growth-archive-content').replaceChildren();el('growth-archive-list').replaceChildren();next=null;
  const value=await call('archives',{after});
  return ()=>{
   next=value.next;el('growth-archive-notice').textContent=value.items.length?'请选择归档查看。':'没有保留的恢复归档。';
   value.items.forEach(identifier=>{
    const button=document.createElement('button');button.className='secondary';button.textContent='查看归档 '+identifier.split(':')[1];
    button.onclick=()=>run(async()=>{
     el('growth-archive-content').replaceChildren();
     const item=await call('archive',{identifier});
     return ()=>{show(el('growth-archive-content'),item.original?.data??item.original);el('growth-archive-notice').textContent='正在查看旧状态，仅供核对；没有恢复或重新授权这份内容。';};
    },'growth-archive-notice');el('growth-archive-list').append(button);
   });
  };
 },'growth-archive-notice');}
 el('growth-archives').onclick=()=>archives('');
 el('growth-archives-next').onclick=()=>{if(next)return archives(next);};
 window.addEventListener('qiban-management-state',({detail})=>{
  const value=detail.ready&&!detail.locked,changed=value!==allowed;allowed=value;el('growth-panel').hidden=el('management-panel').hidden;
  if(changed){epoch++;busy=false;clearProposal();next=null;el('growth-archive-list').replaceChildren();el('growth-archive-content').replaceChildren();el('growth-archive-notice').textContent='';el('growth-notice').textContent=allowed?'尚未检查。':'';}
  controls();
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management'){
   const job=pending.get(data.request_id);if(!job)return;clearTimeout(job.timer);pending.delete(data.request_id);
   if(data.response.ok)job.resolve(data.response.result);else job.reject(new Error(data.response.message||'操作未完成。'));
  }
  if(data.type==='management_error'){for(const job of pending.values()){clearTimeout(job.timer);job.reject(new Error(data.message));}pending.clear();}
 });
 clearProposal();controls();
})();
