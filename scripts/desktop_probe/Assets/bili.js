(()=>{
 'use strict';
 const el=id=>document.getElementById('bili-'+id),requests=new Map();
 let session=null,state=null,busy=false,epoch=0;
 let policyUid=null,policySeq=0,policy=null,edits=[],policyDirty=false,policyBusy=false;
 const labelsByAction={comment:'发表评论',reply:'回复评论',dm_reply:'回复私信',dynamic:'发布动态',like:'视频点赞',
  comment_like:'评论点赞',favorite:'新增收藏',follow:'关注',coin:'投币',favorite_create:'新建收藏夹（个）',favorite_move:'移动收藏视频（个）'};
 const active=()=>['starting','waiting','scanned','verified','confirm','checking'].includes(state?.state);
 const labels={idle:'尚未登录。',starting:'正在请求二维码…',waiting:'请用哔哩哔哩手机应用扫码。',
  scanned:'已扫码，请在手机上确认。',verified:'正在核对绑定账号。',checking:'正在向 B 站核验登录…'};
 function render(){
  el('panel').hidden=!session;
  const account=(prefix,value)=>value?prefix+value.name+'（UID '+value.uid+'）':'';
  el('current').textContent=account('当前绑定：',state?.current);
  el('candidate').textContent=account('本次扫码：',state?.candidate);
  el('candidate').hidden=!state?.candidate;
  el('status').textContent=state?.message||labels[state?.state]||(state?.current?'已绑定。':'');
  if(state?.state==='idle'&&state.current)el('status').textContent='已绑定。';
  el('qr').hidden=!state?.qr;
  if(state?.qr)el('qr').src='data:image/png;base64,'+state.qr;else el('qr').removeAttribute('src');
  el('login').textContent=state?.current?'重新登录':'扫码登录';
  el('login').disabled=!session||busy||active();
  el('confirm').hidden=state?.state!=='confirm';el('confirm').disabled=busy;
  el('cancel').hidden=!active();el('cancel').disabled=busy;
  el('refresh').disabled=!session||busy;
  el('check').disabled=!session||!state?.current||busy||active();
  const uid=state?.current?.uid||null;
  if(uid!==policyUid){
   policyUid=uid;policySeq++;policy=null;edits=[];policyDirty=policyBusy=false;
   el('policy').open=false;el('rules').replaceChildren();el('policy-note').textContent='';
   el('policy-day').textContent='';el('policy-save').disabled=el('policy-default').disabled=true;
  }
  el('policy').hidden=!session||!uid;
 }
 function request(op,args){
  const id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{requests.delete(id);reject(Error('连接超时，请重新读取。'));},12000);
   requests.set(id,{resolve,reject,timer});
   window.chrome.webview.postMessage({action:'management',operation:'bili.'+op,arguments:{session,...args},request_id:id});
  });
 }
 function showPolicy(value){
  policy=value;policyDirty=false;edits=[];const rows=[];
  el('policy-day').textContent='UID '+value.uid+' · '+value.day+'（'+value.timezone+'）';
  for(const [kind,label] of Object.entries(labelsByAction)){
   const row=document.createElement('tr'),enabled=document.createElement('input'),limit=document.createElement('input');
   enabled.type='checkbox';enabled.checked=value.actions[kind].enabled;enabled.setAttribute('aria-label','启用'+label);
   limit.type='number';limit.min=0;limit.max=1000;limit.step=1;limit.required=true;limit.value=value.actions[kind].daily_limit;
   limit.setAttribute('aria-label',label+'日限额');edits.push({kind,enabled,limit});
   enabled.onchange=limit.oninput=()=>{policyDirty=true;el('policy-note').textContent='修改后点击保存生效。';};
   for(const item of [label,enabled,limit,String(value.used[kind]),String(Math.max(0,value.actions[kind].daily_limit-value.used[kind]))]){
    const cell=document.createElement('td');if(typeof item==='string')cell.textContent=item;else cell.append(item);row.append(cell);
   }
   rows.push(row);
  }
  el('rules').replaceChildren(...rows);
 }
 async function policyRequest(save){
  if(!session||!policyUid||policyBusy||save&&!policy)return;
  const seq=++policySeq,view=epoch,args={uid:policyUid};
  if(save){
   args.expected=policy.revision;args.actions={};
   for(const {kind,enabled,limit} of edits){
    if(!limit.reportValidity())return;
    args.actions[kind]={enabled:enabled.checked,daily_limit:Number(limit.value)};
   }
  }
  policyBusy=true;el('policy-save').disabled=el('policy-read').disabled=el('policy-default').disabled=true;
  for(const {enabled,limit} of edits)enabled.disabled=limit.disabled=true;
  try{
   const result=await request(save?'policy_save':'policy',args);
   if(seq!==policySeq||view!==epoch)return;
   showPolicy(result);el('policy-note').textContent=save?'互动设置已保存。':'';
  }catch(error){if(seq===policySeq&&view===epoch)el('policy-note').textContent=error.message;}
  finally{if(seq===policySeq&&view===epoch){
   policyBusy=false;el('policy-read').disabled=false;el('policy-save').disabled=el('policy-default').disabled=!policy;
   for(const {enabled,limit} of edits)enabled.disabled=limit.disabled=false;
  }}
 }
 el('policy').ontoggle=()=>{if(el('policy').open&&!policyDirty)void policyRequest(false);};
 el('policy-save').onclick=()=>void policyRequest(true);
 el('policy-default').onclick=()=>{
  if(!policy||policyBusy)return;
  for(const {kind,enabled,limit} of edits){
   enabled.checked=policy.defaults[kind].enabled;limit.value=policy.defaults[kind].daily_limit;
  }
  policyDirty=true;el('policy-note').textContent='已填入默认值，点击保存后生效。';
 };
 el('policy-read').onclick=()=>void policyRequest(false);
 async function run(op){
  if(!session||busy)return;
  const view=epoch;
  busy=true;render();
  try{
   const args={};
   if(op==='cancel'||op==='confirm')args.ticket=state.ticket;
   const value=await request(op,args);
   if(view===epoch)state=value;
  }catch(error){if(view===epoch){el('status').textContent=error.message;state={...state,message:error.message};}}
  finally{if(view===epoch){busy=false;render();}}
 }
 for(const [id,op] of [['login','login'],['cancel','cancel'],['confirm','confirm'],['check','check'],['refresh','status']]){
  el(id).onclick=()=>void run(op);
 }
 window.addEventListener('qiban-management-state',({detail})=>{
  const current=detail.ready&&!detail.locked?detail.roomSession:null;
  if(current===session)return;
  epoch++;session=current;state=null;busy=false;render();
  if(session)void run('status');
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management_error'){
   for(const job of requests.values()){clearTimeout(job.timer);job.reject(Error(data.message));}requests.clear();
  }else if(data.type==='management'){
   const job=requests.get(data.request_id);if(!job)return;
   clearTimeout(job.timer);requests.delete(data.request_id);
   if(data.response.ok)job.resolve(data.response.result);else job.reject(Error(data.response.message));
  }
 });
 setInterval(()=>{if(active())void run('status');},1200);
 render();
})();
