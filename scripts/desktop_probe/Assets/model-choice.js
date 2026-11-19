(()=>{
 'use strict';
 const el=id=>document.getElementById(id),pending=new Map();
 let allowed=false,busy=false,state=null,epoch=0,entered=false,account=null,authorizationReady=false,saveTimer=null,noticeTimer=null;
 const status=text=>el('model-choice-status').textContent=text;
 const chosen=()=>state?.models.find(row=>row.id===el('model-choice').value);
 function controls(){
  const blocked=!allowed||busy,row=chosen();
  el('model-catalog-refresh').disabled=blocked;
  el('model-choice').disabled=blocked||!state?.models.length;
  el('model-effort').disabled=blocked||!row?.efforts.length;
  el('model-choice-save').disabled=blocked||!row;
  for(const button of el('model-effort-choices').children)button.disabled=blocked;
  window.QibanFeedback?.mark(el('model-catalog-refresh'),busy);
 }
 function call(operation,args){const request_id=crypto.randomUUID().replaceAll('-','');return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{pending.delete(request_id);reject(Object.assign(Error('模型操作超时，请重新读取。'),{code:'timeout'}));},35000);
  pending.set(request_id,{resolve,reject,timer});window.chrome.webview.postMessage({action:'management',operation:'model.'+operation,arguments:args,request_id});
 });}
 const titles={minimal:'最少',low:'低',medium:'中',high:'高',xhigh:'更高',max:'最高',ultra:'超高',none:'关闭'};
 function efforts(value){const row=state?.models.find(r=>r.id===el('model-choice').value);el('model-effort').replaceChildren();
  if(row?.efforts.length===0){
   const option=document.createElement('option');option.value='';option.textContent='由模型决定';el('model-effort').append(option);
  }
  for(const effort of row?.efforts||[]){const option=document.createElement('option');option.value=effort;option.textContent=titles[effort]||effort;el('model-effort').append(option);}
  el('model-effort').value=row?.efforts.includes(value)?value:row?.default_effort||'';
  const choices=el('model-effort-choices');choices.replaceChildren();
  el('model-effort-fallback').hidden=!!row?.efforts.length;
  if(!row?.efforts.length)return;
  const heading=document.createElement('p');heading.textContent='思考强度';choices.append(heading);
  for(const effort of row.efforts){
   const b=document.createElement('button');b.type='button';b.className='secondary';b.textContent=titles[effort]||effort;
   b.setAttribute('aria-pressed',String(effort===el('model-effort').value));
   b.onclick=()=>{if(busy||!allowed)return;efforts(effort);controls();scheduleSave();};
   choices.append(b);
  }
 }
 function paint(value){state=value;el('model-choice').replaceChildren();for(const row of value.models){const o=document.createElement('option');o.value=row.id;o.textContent=row.name;el('model-choice').append(o);}
  el('model-choice').value=value.model;efforts(value.effort);
  const saved=value.models.find(m=>m.id===value.model);
  el('model-applied').textContent=value.model?(saved?.name||value.model)+' · '+(value.effort?(titles[value.effort]||value.effort):'由模型决定'):'尚未选择';
  el('model-count').textContent=value.models.length+' 个可用模型';
  status(value.available?'':'原模型已不可用，请重新选择。');
 }
 async function run(save=false,refresh=false){const row=chosen();if(!allowed||busy||(save&&!row))return;if(save)clearTimeout(saveTimer);const current=epoch;busy=true;controls();status(save?'正在保存…':'正在读取模型列表…');
  try{const result=await call(save?'configure':'catalog',save?{account_id:state.account_id,expected:state.revision,model:row.id,effort:row.efforts.length?el('model-effort').value:null}:{refresh});
   if(allowed&&epoch===current){paint(result);if(save){status('已保存');clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>{if(epoch===current)status('');},2500);}el('model-catalog-refresh').textContent='刷新模型目录';}
  }catch(error){if(epoch===current){if(save&&state)paint(state);status((save?'保存失败。':'')+error.message);el('model-catalog-refresh').hidden=false;el('model-catalog-refresh').textContent='重试';
   if(save&&['edit_conflict','account_changed','timeout'].includes(error.code)){
    try{const fresh=await call('catalog',{refresh:true});if(epoch===current&&allowed){paint(fresh);status('已重新读取当前账号的设置，请核对后选择。');}}
    catch(reload){if(epoch===current)status('保存结果待核对。'+reload.message);}
   }
  }}finally{if(epoch===current){busy=false;controls();}}
 }
 function enter(){const visible=allowed&&document.body.dataset.navPage==='model';if(visible&&!entered){entered=true;void run();}else if(!visible)entered=false;}
 function scheduleSave(){clearTimeout(saveTimer);clearTimeout(noticeTimer);const view=epoch;status('待保存…');saveTimer=setTimeout(()=>{if(view===epoch&&allowed)void run(true);},350);}
 el('model-effort').onchange=scheduleSave;
 el('model-choice').onchange=()=>{efforts();controls();scheduleSave();};el('model-catalog-refresh').onclick=()=>run(false,true);el('model-choice-save').onclick=()=>run(true);
 window.addEventListener('qiban-nav-change',enter);
 function clearSelection(){
  state=null;el('model-choice').replaceChildren();el('model-effort').replaceChildren();el('model-effort-choices').replaceChildren();
  el('model-applied').textContent=el('model-count').textContent='等待读取';
 }
 window.addEventListener('qiban-management-state',({detail})=>{allowed=detail.ready&&!detail.locked;if(!allowed){clearTimeout(saveTimer);clearTimeout(noticeTimer);epoch++;clearSelection();busy=false;status('解锁后选择模型。');}controls();enter();});
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management'){
   const result=data.response?.result;
   if(result?.multi_account&&result.active_account!==undefined){
    const ready=result.state==='authenticated'&&!result.busy&&!result.token_expired&&result.subscription_permission!==false;
    const reload=result.active_account!==account||(ready&&!authorizationReady);
    account=result.active_account;authorizationReady=ready;
    if(reload){clearTimeout(saveTimer);clearTimeout(noticeTimer);epoch++;clearSelection();busy=false;entered=false;controls();enter();}
   }
   const item=pending.get(data.request_id);if(!item)return;clearTimeout(item.timer);pending.delete(data.request_id);
   if(data.response.ok)item.resolve(result);else item.reject(Object.assign(Error(data.response.message||'模型设置未完成。'),{code:data.response.error}));
  }else if(data.type==='management_error'){for(const item of pending.values()){clearTimeout(item.timer);item.reject(Error(data.message));}pending.clear();}
 });controls();
})();
