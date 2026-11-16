// Shared entry-driven reads. Business modules keep their own authorization,
// revision and draft rules; this layer never submits a save or starts a platform.
(()=>{
 'use strict';
 const entries=new Map(),requests=new Map();let allowed=false,active=null,generation=0;
 const bridge=window.chrome?.webview;
 function visible(button){
  const panel=button.closest('section')||button.parentElement;
  if(!panel||panel.hidden||!panel.getClientRects().length)return false;
  for(let p=button.parentElement;p;p=p.parentElement)if(p.tagName==='DETAILS'&&!p.open)return false;
  return true;
 }
 function failed(entry,epoch=generation){if(!allowed||epoch!==generation)return;entry.button.dataset.autoError='true';entry.button.textContent='重试';}
 function invoke(entry){
  if(!allowed||entry.button.disabled||entry.dirty())return false;
  entry.button.removeAttribute('data-auto-error');entry.entered=true;
  const before=active,epoch=generation;active=entry;
  try{Promise.resolve(entry.read()).catch(()=>failed(entry,epoch));}catch(_){failed(entry,epoch);}
  finally{active=before;}
  return true;
 }
 function check(){
  for(const entry of entries.values()){
   if(!allowed||!visible(entry.button)){entry.entered=false;continue;}
   if(!entry.entered)invoke(entry);
  }
 }
 function register(id,{dirty=()=>false,read}={}){
  const button=document.getElementById(id);if(!button)return;
  const original=button.onclick;
  const entry={button,dirty,read:read||(()=>original?.()),entered:false};
  button.onclick=()=>invoke(entry);
  button.dataset.autoRead='true';entries.set(id,entry);
 }
 if(bridge){
  const send=bridge.postMessage.bind(bridge);
  bridge.postMessage=message=>{
   if(active&&message.request_id){
    const entry=active,epoch=generation,timer=setTimeout(()=>{requests.delete(message.request_id);failed(entry,epoch);},40000);
    requests.set(message.request_id,{entry,timer});
   }
   return send(message);
  };
  bridge.addEventListener('message',({data})=>{
   const pending=requests.get(data.request_id);
   if(pending){clearTimeout(pending.timer);requests.delete(data.request_id);if(data.response?.ok===false)failed(pending.entry);}
   if(data.type==='management_error'){for(const {entry,timer} of requests.values()){clearTimeout(timer);failed(entry);}requests.clear();}
  });
 }
 const schedule=()=>queueMicrotask(()=>queueMicrotask(check));
 window.QibanAutoRead={register,settled:schedule,changed(id){const entry=entries.get(id);if(entry){entry.entered=false;queueMicrotask(check);}}};
 for(const name of ['qiban-nav-change','qiban-tab-change'])window.addEventListener(name,schedule);
 window.addEventListener('qiban-management-state',({detail})=>{allowed=!!detail.ready&&!detail.locked;if(!allowed){generation++;for(const entry of entries.values()){entry.entered=false;entry.button.removeAttribute('data-auto-error');}for(const {timer} of requests.values())clearTimeout(timer);requests.clear();}schedule();});
 document.addEventListener('toggle',()=>queueMicrotask(check),true);
})();
