(()=>{
 'use strict';
 const el=id=>document.getElementById(id),pending=new Map();
 let allowed=false,busy=false,state=null,epoch=0,entered=false,account=null;
 const status=text=>el('model-choice-status').textContent=text;
 function controls(){for(const id of ['model-choice','model-effort','model-choice-save','model-catalog-refresh'])el(id).disabled=!allowed||busy||(id!=='model-catalog-refresh'&&!state);window.QibanFeedback?.mark(el('model-catalog-refresh'),busy);}
 function call(operation,args){const request_id=crypto.randomUUID().replaceAll('-','');return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{pending.delete(request_id);reject(Error('模型列表读取超时，请重试。'));},35000);
  pending.set(request_id,{resolve,reject,timer});window.chrome.webview.postMessage({action:'management',operation:'model.'+operation,arguments:args,request_id});
 });}
 const titles={minimal:'最少',low:'低',medium:'中',high:'高',xhigh:'更高',max:'最高',ultra:'超高',none:'关闭'};
 function efforts(value){const row=state?.models.find(r=>r.id===el('model-choice').value);el('model-effort').replaceChildren();
  for(const effort of row?.efforts||[]){const option=document.createElement('option');option.value=effort;option.textContent=titles[effort]||effort;el('model-effort').append(option);}
  el('model-effort').value=row?.efforts.includes(value)?value:row?.default_effort||'';
 }
 function paint(value){state=value;el('model-choice').replaceChildren();for(const row of value.models){const o=document.createElement('option');o.value=row.id;o.textContent=row.name;el('model-choice').append(o);}
  el('model-choice').value=value.model;efforts(value.effort);
  status(value.available?'当前：'+(value.models.find(r=>r.id===value.model)?.name||value.model)+' · '+(titles[value.effort]||value.effort):'原模型已不可用，请重新选择。');
 }
 async function run(save=false,refresh=false){if(!allowed||busy)return;const current=epoch;busy=true;controls();status(save?'正在保存…':'正在读取模型列表…');
  try{const result=await call(save?'configure':'catalog',save?{account_id:state.account_id,expected:state.revision,model:el('model-choice').value,effort:el('model-effort').value}:{refresh});
   if(allowed&&epoch===current){paint(result);if(save)status('已保存，新任务使用所选模型和思考强度。');}
  }catch(error){if(epoch===current)status(error.message);}finally{if(epoch===current){busy=false;controls();}}
 }
 function enter(){const visible=allowed&&document.body.dataset.navPage==='model';if(visible&&!entered){entered=true;void run();}else if(!visible)entered=false;}
 el('model-choice').onchange=()=>efforts();el('model-catalog-refresh').onclick=()=>run(false,true);el('model-choice-save').onclick=()=>run(true);
 window.addEventListener('qiban-nav-change',enter);
 window.addEventListener('qiban-management-state',({detail})=>{allowed=detail.ready&&!detail.locked;if(!allowed){epoch++;state=null;busy=false;el('model-choice').replaceChildren();el('model-effort').replaceChildren();status('解锁后选择模型。');}controls();enter();});
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management'){
   const result=data.response?.result;
   if(result?.multi_account&&result.active_account!==undefined&&result.active_account!==account){account=result.active_account;epoch++;state=null;busy=false;entered=false;controls();enter();}
   const item=pending.get(data.request_id);if(!item)return;clearTimeout(item.timer);pending.delete(data.request_id);
   if(data.response.ok)item.resolve(result);else item.reject(Error(data.response.message||'模型设置未完成。'));
  }else if(data.type==='management_error'){for(const item of pending.values()){clearTimeout(item.timer);item.reject(Error(data.message));}pending.clear();}
 });controls();
})();
