/* Shared lifecycle for the authenticated learning and context pages. */
(() => {
 'use strict';
 const bridge=window.chrome?.webview;
 const factories={},pending=new Map();
 let enabled=false,page='',root=null,controller=null,capabilities=null,session=null;
 const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const option=(value,label,selected=false)=>'<option value="'+escape(value)+'"'+(selected?' selected':'')+'>'+escape(label)+'</option>';
 const button=(id,label,disabled=false)=>'<button type="button" data-ws="'+id+'" class="secondary"'+(disabled?' disabled':'')+'>'+escape(label)+'</button>';
 const names={local:'会客室',qq:'QQ',wechat:'微信',bili_private:'B 站私聊',private:'私聊',group:'群聊',public:'公开场景',memory_records:'记忆',summary:'会话摘要',relations:'人物关系',growth:'成长记录',catalog:'资料目录',files:'文件',stickers:'表情',personal_facts:'个人事实',private_scope:'仅限私聊',reply_context_chars:'上下文字数',reply_context_tokens:'估算 token 上限',reply_output_reserve:'输出预留'};
 const label=id=>names[id]||String(id??'');
 const aborted=()=>new DOMException('页面已切换','AbortError');
 function call(operation,args,signal){
  if(signal.aborted||!enabled)return Promise.reject(aborted());
  return new Promise((resolve,reject)=>{
   const id=crypto.randomUUID().replaceAll('-','');
   const finish=(fn,v)=>{clearTimeout(timer);signal.removeEventListener('abort',cancel);pending.delete(id);fn(v);};
   const cancel=()=>finish(reject,aborted());
   const timer=setTimeout(()=>finish(reject,Object.assign(new Error('请求超时，请重新读取以核对结果。'),{code:'timeout'})),25000);
   signal.addEventListener('abort',cancel,{once:true});
   pending.set(id,{resolve:v=>finish(resolve,v),reject:e=>finish(reject,e)});
   try{bridge.postMessage({action:'management',operation,arguments:args,request_id:id});}
   catch(e){finish(reject,e);}
  });
 }
 function clear(){
  controller?.abort();controller=null;
  if(root){root.replaceChildren();root.onclick=root.onchange=root.oninput=null;}
 }
 function setAccess(detail){
  const next=!!detail?.ready&&!detail.locked;
  const identity=next?detail.roomSession??null:null;
  if(next===enabled&&(!identity||identity===session))return;
  enabled=next;session=identity;capabilities=null;
  if(root)open(page,root);
 }
 bridge?.addEventListener('message',({data})=>{
  if(data.type==='management_error'){
   for(const task of [...pending.values()])task.reject(new Error('桌面后台已断开。'));
   setAccess({ready:false});
   return;
  }
  if(data.type!=='management')return;
  const task=pending.get(data.request_id);
  if(!task)return;
  const reply=data.response||{};
  if(reply.ok){task.resolve(reply.result);return;}
  task.reject(Object.assign(new Error(reply.message||'操作未完成。'),{code:reply.error}));
  if(['desktop_locked','admin_required'].includes(reply.error))setAccess({ready:false});
 });
 async function open(key,target){
  clear();page=key;root=target;
  if(!root)return;
  root.classList.add('ws');
  root.innerHTML='<h2>'+({learn:'学习树',context:'上下文工作台'}[key]||'')+'</h2><p class="ws-status" role="status"></p><div class="ws-body"></div>';
  const message=root.querySelector('.ws-status'),body=root.querySelector('.ws-body');
  if(!enabled){message.textContent='解锁桌面管理后可查看。';return;}
  const current=controller=new AbortController(),signal=current.signal;
  const notice=text=>{if(!signal.aborted)message.textContent=text;};
  const showError=e=>{if(!signal.aborted&&e.name!=='AbortError')notice(e.message);};
  try{
   notice('正在读取可用功能…');
   let caps=capabilities;
   if(!caps){
    try{caps=await call('workspace.capabilities',{},signal);}
    catch(error){
     if(['management_request','unsupported','unknown_operation'].includes(error.code))throw Error('当前后台版本尚未提供工作台。');
     throw error;
    }
   }
   if(signal.aborted)return;
   if(!Array.isArray(caps.operations))throw Error('此版本未提供工作台能力清单。');
   capabilities=caps;
   const has=op=>caps.operations.includes(op)||(caps.trace_detail==='reply_trace.detail'&&['reply_trace.list','reply_trace.detail'].includes(op));
   if(!has(key==='learn'?'learning.goals':'context.workspace')){notice('当前后台尚未提供此功能。');return;}
   notice('');
   const request=(op,args={})=>has(op)?call(op,args,signal):Promise.reject(new Error('当前后台未提供此操作。'));
   const reset=()=>{if(!signal.aborted){capabilities=null;return open(key,target);}};
   await factories[key]({body,signal,caps,has,request,notice,showError,reset});
  }catch(e){
   if(signal.aborted)return;
   showError(e);body.innerHTML=button('retry','重新读取');body.onclick=()=>open(key,target);
  }
 }
 function nativeNav(){
  const panel=!document.body.dataset.roomOpen&&document.getElementById('workspace-'+document.body.dataset.navPage);
  if(panel&&factories[document.body.dataset.navPage]){if(root!==panel)open(document.body.dataset.navPage,panel);}
  else if(root?.id?.startsWith('workspace-')){clear();root=null;page='';}
 }
 window.QibanWorkspace={register:(name,f)=>{factories[name]=f;},open,close(){clear();root=null;page='';},setAccess,escape,option,button,label};
 window.addEventListener('qiban-management-state',({detail})=>setAccess(detail));
 window.addEventListener('qiban-nav-change',nativeNav);
 window.addEventListener('qiban-room-view',nativeNav);
})();
