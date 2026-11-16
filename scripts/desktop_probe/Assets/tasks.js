(()=>{
 'use strict';
 const el=id=>document.getElementById('tasks-'+id),pending=new Map();
 let session=null,epoch=0,busy=false,nonce=null,timer,reading=false,selected=null,next=null,resultId=null,canSubmit=false,refreshPending=false,capabilities={};
 function call(operation,args={}){
  const request_id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timeout=setTimeout(()=>{pending.delete(request_id);reject(Error('请求超时，请重新读取任务确认。'));},15000);
   pending.set(request_id,{resolve,reject,timeout});
   window.chrome.webview.postMessage({action:'management',operation,arguments:{session,...args},request_id});
  });
 }
 function controls(available=canSubmit){
  el('submit').disabled=!session||busy||!available;
  el('refresh').disabled=!session||busy;
  el('more').disabled=!session||busy||next===null;
  for(const id of ['kind','prompt','criteria','rounds','bvid','page','start','end','mode','reason'])el(id).disabled=!session||busy;
 }
 function kind(){
  const selected=el('kind').value||'video',capability=capabilities[selected];
  el('video-fields').hidden=selected!=='video';el('web-fields').hidden=selected==='video';el('learn-fields').hidden=selected!=='learn';
  el('submit').textContent={video:'提交视频任务',web:'提交查资料任务',learn:'提交学习任务'}[selected];
  canSubmit=!!capability?.available;
  el('availability').textContent=!session?'请先启动后台并解锁本机管理。':capability?.reason||(selected==='video'?'视频任务可提交；仅执行此次要求，使用现有任务与模型额度。':'任务可提交。');
  controls();
 }
 const states={queued:'等待执行',running:'正在执行',completed:'已完成',failed:'未完成',cancelled:'已取消'};
 function render(items){
  el('items').replaceChildren();
  for(const item of items){
   const row=document.createElement('article'),heading=document.createElement('h3'),summary=document.createElement('p');
   heading.textContent=(item.arguments.bvid||item.arguments.prompt||item.arguments.objective)+' · '+(states[item.status]||item.status);
   summary.textContent=item.result?.summary||'';
   row.append(heading,summary);
   if(item.error){const error=document.createElement('p');error.textContent='停止原因：'+item.error;row.append(error);}
   const view=document.createElement('button');view.textContent='查看结果';view.className='secondary';
   view.onclick=()=>action(async()=>({detail:await call('tasks.detail',{identifier:item.id,offset:0}),identifier:item.id,append:false}));
   row.append(view);
   if(['queued','running','failed','cancelled'].includes(item.status)){
    const button=document.createElement('button'),cancel=['queued','running'].includes(item.status);
    button.textContent=cancel?'取消任务':'重试未完成步骤';button.className='secondary';
    button.onclick=()=>action(async()=>{await call('tasks.control',{identifier:item.id,action:cancel?'cancel':'retry',expected:item.revision});return {notice:cancel?'任务已取消。':'任务已重新排队，已保存步骤保留。'};});
    row.append(button);
   }
   el('items').append(row);
  }
  if(!items.length)el('items').textContent='尚无手动任务。';
 }
 async function refresh(){
  if(!session||reading||busy)return;
  const view=epoch;reading=true;
  try{
   const data=await call('tasks.list');if(view!==epoch)return;
   render(data.items);capabilities=data.capabilities;kind();
   clearTimeout(timer);
   if(data.items.some(item=>['queued','running'].includes(item.status)))timer=setTimeout(refresh,2500);
  }catch(error){if(view===epoch)el('notice').textContent=error.message;}
  finally{if(view===epoch){reading=false;if(refreshPending){refreshPending=false;void refresh();}}}
 }
 async function action(work){
  if(!session||busy)return;
  const view=epoch;busy=true;controls();
  try{
   const result=await work();if(view!==epoch)return;
   if(result.notice)el('notice').textContent=result.notice;
   if(result.text!==undefined)el('result').textContent=result.text;
   if(result.detail){
    if(result.append&&resultId!==result.detail.result_id)throw Error('任务已产生新结果，请重新点击查看结果。');
    selected=result.identifier;resultId=result.detail.result_id;next=result.detail.next;
    el('result').textContent=(result.append?el('result').textContent:'')+result.detail.text;
   }
  }catch(error){if(view===epoch)el('notice').textContent=error.message;}
  finally{if(view===epoch){busy=false;controls();if(reading)refreshPending=true;else void refresh();}}
 }
 el('submit').onclick=()=>action(async()=>{
  const selectedKind=el('kind').value||'video';
  let arguments_;
  if(selectedKind==='video'){
  arguments_={bvid:el('bvid').value.trim(),page_index:Number(el('page').value)-1,
   start_seconds:Number(el('start').value),end_seconds:Number(el('end').value),mode:el('mode').value,reason:el('reason').value.trim()};
  if(!/^BV[0-9A-Za-z]{10}$/.test(arguments_.bvid)||!arguments_.reason||!Number.isInteger(arguments_.page_index)||arguments_.page_index<0||arguments_.page_index>999||!Number.isFinite(arguments_.start_seconds)||!Number.isFinite(arguments_.end_seconds)||arguments_.start_seconds<0||arguments_.end_seconds>86400||arguments_.end_seconds<=arguments_.start_seconds)throw Error('请填写有效 BV 号、分集、时间范围和阅读要求。');
  }else{
   const objective=el('prompt').value.trim();if(!objective)throw Error('请填写查资料的问题或学习目标。');
   arguments_={prompt:objective};
   if(selectedKind==='learn'){
    const criteria=el('criteria').value.split('\n').map(item=>item.trim()).filter(Boolean),round_limit=Number(el('rounds').value);
    if(criteria.length<1||criteria.length>6||criteria.some(item=>item.length>500)||!Number.isInteger(round_limit)||round_limit<1||round_limit>6)throw Error('请填写 1—6 项检查标准，每项最多 500 字；学习轮数为 1—6。');
    arguments_={objective,criteria,round_limit};
   }
  }
  nonce=nonce||crypto.randomUUID().replaceAll('-','');
  await call('tasks.submit',{kind:selectedKind,arguments:arguments_,nonce});nonce=null;
  return {notice:'任务已提交，可在下方查看进度。'};
 });
 for(const id of ['prompt','criteria','rounds','bvid','page','start','end','mode','reason'])el(id).addEventListener('input',()=>{nonce=null;});
 el('kind').addEventListener('change',()=>{nonce=null;kind();});
 el('refresh').onclick=refresh;window.QibanAutoRead?.register('tasks-refresh');
 el('more').onclick=()=>action(async()=>({detail:await call('tasks.detail',{identifier:selected,offset:next}),identifier:selected,append:true}));
 window.addEventListener('qiban-management-state',({detail})=>{
  const newSession=detail.ready&&!detail.locked?detail.roomSession:null;
  if(newSession===session)return;
  session=newSession;epoch++;busy=reading=canSubmit=refreshPending=false;capabilities={};nonce=null;selected=null;resultId=null;next=null;clearTimeout(timer);
  for(const id of ['bvid','reason','prompt','criteria'])el(id).value='';
  for(const [id,value] of Object.entries({page:'1',start:'0',end:'60',rounds:'1',mode:'text',kind:'video'}))el(id).value=value;
  el('items').replaceChildren();el('result').textContent='';el('notice').textContent='';controls();
  kind();
  if(session)void refresh();
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management_error'){
   for(const job of pending.values()){clearTimeout(job.timeout);job.reject(Error(data.message));}
   pending.clear();return;
  }
  const job=pending.get(data.request_id);if(!job||data.type!=='management')return;
  pending.delete(data.request_id);clearTimeout(job.timeout);
  if(data.response.ok)job.resolve(data.response.result);else job.reject(Error(data.response.message));
 });
 controls();
})();
