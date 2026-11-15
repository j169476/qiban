// SPDX-License-Identifier: MIT
(()=>{
 'use strict';
 const counts=new WeakMap(), jobs=new Map(), nativeJobs=new Map();
 const nativeTypes={'wechat-client-login':'wechat-login','uninstall-backup':'uninstall',start:'service',stop:'service',pick:'file'};
 let clicked=null;
 function paint(button){
  if(!button?.matches?.('button'))return;
  const busy=(counts.get(button)||0)>0||button.dataset.operationBusy==='true';
  button.classList.toggle('is-loading',busy);
  button.setAttribute('aria-busy',String(busy));
 }
 function start(button){
  if(!button?.matches?.('button'))return ()=>{};
  counts.set(button,(counts.get(button)||0)+1);paint(button);
  let done=false;
  return ()=>{if(done)return;done=true;counts.set(button,Math.max(0,(counts.get(button)||0)-1));paint(button);};
 }
 window.QibanFeedback={start,mark(button,busy){if(button){button.dataset.operationBusy=String(busy);paint(button);}}};
 document.addEventListener('click',event=>{
  clicked=event.target.closest('button');
  const current=clicked;
  setTimeout(()=>{if(clicked===current)clicked=null;},0);
 },true);
 const bridge=window.chrome?.webview;
 if(!bridge)return;
 const send=bridge.postMessage.bind(bridge);
 // Associate requests only with the initiating click, never with background polling.
 bridge.postMessage=message=>{
  const nativeType=nativeTypes[message.action];
  if(clicked&&nativeType){
   nativeJobs.get(nativeType)?.();
   const finish=start(clicked),timer=setTimeout(()=>{nativeJobs.delete(nativeType);finish();},260000);
   nativeJobs.set(nativeType,()=>{clearTimeout(timer);finish();});
  }
  if(clicked&&message.request_id){
   const finish=start(clicked),id=message.request_id;
   const timer=setTimeout(()=>{jobs.delete(id);finish();},260000);
   jobs.set(id,()=>{clearTimeout(timer);finish();});
  }
  try{return send(message);}catch(error){jobs.get(message.request_id)?.();jobs.delete(message.request_id);throw error;}
 };
 bridge.addEventListener('message',({data})=>{
  if(nativeJobs.has(data.type)&&!(data.type==='service'&&['starting','stopping'].includes(data.state))&&
     !(data.type==='uninstall'&&data.state?.backup==='running')){
   nativeJobs.get(data.type)();nativeJobs.delete(data.type);
  }
  if(data.type==='management_error'){
   for(const finish of jobs.values())finish();jobs.clear();return;
  }
  if(data.request_id){jobs.get(data.request_id)?.();jobs.delete(data.request_id);}
 });
})();
