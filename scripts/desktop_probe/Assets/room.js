(()=>{
 'use strict';
 const el=id=>document.getElementById('room-'+id);
 const pending=new Map();
 let session=null,epoch=0,pageRequest=0,before='',next='',posting=false,nonce=null,timer;
 let pictures=[],selecting=false,displayed=null,reading=false;
 let query='',queueRequest=0;
 const cancelling=new Set();
 function controls(){
  el('send').disabled=posting||selecting||(!el('text').value.trim()&&!pictures.length);
  el('pick').disabled=posting||selecting||pictures.length===3;
  el('text').disabled=posting;
 }
 function draft(){
  const rows=pictures.map(({file},i)=>{
   const row=document.createElement('div'),name=document.createElement('span'),remove=document.createElement('button');
   name.textContent=file.name;remove.textContent='移除';remove.className='secondary';remove.disabled=posting||selecting;
   remove.setAttribute('aria-label','移除 '+file.name);
   remove.onclick=()=>{pictures.splice(i,1);nonce=null;draft();};
   row.append(name,remove);return row;
  });
  el('draft-images').replaceChildren(...rows);controls();
 }
 function reset(){
  epoch++;pageRequest++;before=next='';nonce=null;posting=false;clearInterval(timer);
  pictures=[];selecting=false;displayed=null;reading=false;el('pick').value='';el('text').disabled=false;
  query='';queueRequest++;cancelling.clear();el('query').value='';el('queue').open=false;el('jobs').replaceChildren();
  el('draft-images').replaceChildren();el('pick').disabled=false;
  el('history').replaceChildren();el('text').value='';el('notice').textContent='';
  el('older').disabled=true;el('send').disabled=true;
 }
 function close(){
  reset();
  if(el('dialog').open)el('dialog').close();
  el('open').removeAttribute('aria-current');
  if(document.body)delete document.body.dataset.roomOpen;
 }
 function call(operation,args,files){
  const id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timeout=setTimeout(()=>{pending.delete(id);reject(Error('请求超时，请刷新确认。'));},12000);
   pending.set(id,{resolve,reject,timeout});
   const message={action:'management',operation,arguments:{session,...args},request_id:id};
   try{
    if(files)window.chrome.webview.postMessageWithAdditionalObjects(message,files);
    else window.chrome.webview.postMessage(message);
   }catch(error){pending.delete(id);clearTimeout(timeout);reject(error);}
  });
 }
 async function history(cursor='',quiet=false){
  const view=epoch, seq=++pageRequest;
  reading=true;
  try{
   const data=query?await call('room.search',{before:cursor,query}):await call('room.history',{before:cursor});
   if(epoch!==view||seq!==pageRequest)return;
   before=cursor;next=data.next;el('older').disabled=!next;
   const signature=JSON.stringify([query,data.messages,data.next]);
   if(signature===displayed)return;
   displayed=signature;
   const rows=data.messages.map(message=>{
    const row=document.createElement('article'),label=document.createElement('strong'),body=document.createElement('p');
    row.className=message.speaker==='assistant'?'room-answer':'room-message';
    label.textContent=message.speaker==='assistant'?'栖伴':'我';body.textContent=message.text;
    row.append(label,body);
    for(const [index,attachment] of (message.attachments||[]).entries()){
     const match=/^input-image:([a-f0-9]{32})$/.exec(attachment.url||'');
     if(!match)continue;
     const button=document.createElement('button');button.className='secondary';button.textContent='查看图片 '+(index+1);
     button.onclick=async()=>{
      button.disabled=true;
      try{
       const result=await call('room.preview',{turn:message.id,image:match[1]});
       if(view!==epoch||!button.isConnected)return;
       const img=document.createElement('img');img.src='data:image/jpeg;base64,'+result.data;img.alt='消息图片 '+(index+1);
       button.replaceWith(img);
      }catch(error){if(view===epoch){button.disabled=false;el('notice').textContent=error.message;}}
     };
     row.append(button);
    }
    return row;
   });
   if(!rows.length){const empty=document.createElement('p');empty.textContent=query?
    (data.next?'本页没有匹配的消息，可继续查找更早的消息。':'没有找到匹配的消息。'):'当前没有聊天记录。';rows.push(empty);}
   el('history').replaceChildren(...rows);
  }catch(error){if(view===epoch&&seq===pageRequest){
   displayed=null;el('history').replaceChildren();if(!quiet)el('notice').textContent=error.message;
  }}finally{if(view===epoch&&seq===pageRequest)reading=false;}
 }
 el('open').onclick=()=>{
  if(!session||el('dialog').open)return;
  reset();
  if(document.body)document.body.dataset.roomOpen='true';
  el('open').setAttribute('aria-current','page');
  el('dialog').show();
  if(typeof window.scrollTo==='function')window.scrollTo({top:0,behavior:'instant'});
  el('queue').open=true;
  void history();
  timer=setInterval(()=>{if(!reading&&!posting)void history(before,true);if(el('queue').open)void queue();},5000);
 };
 function returnToManagement(){
  close();
  el('open').focus?.();
 }
 el('close').onclick=returnToManagement;
 el('dialog').addEventListener('cancel',event=>{event.preventDefault();returnToManagement();});
 // 非模态 dialog 不派发 cancel，Esc 由这里按原来的 close/reset 处理；菜单开着时 navigation.js
 // 已在捕获阶段消费这次 Esc，所以只会收起菜单。
 if(typeof document.addEventListener==='function')document.addEventListener('keydown',event=>{
  if(event.key!=='Escape'&&event.key!=='Esc')return;
  if(event.defaultPrevented||!el('dialog').open)return;
  event.preventDefault();returnToManagement();
 });
 // 离开分区时按原 close/reset 关闭，避免后台隐藏的输入继续接收。
 window.addEventListener('qiban-nav-change',()=>{if(el('dialog').open)close();});
 el('lock').onclick=()=>document.getElementById('lock-now').click();
 el('older').onclick=()=>{if(next)void history(next);};
 el('latest').onclick=()=>{query='';el('query').value='';void history();};
 el('search').onclick=()=>{query=el('query').value.trim();void history();};
 el('query').onkeydown=e=>{if(e.key==='Enter'&&!e.isComposing){e.preventDefault();el('search').onclick();}};
 const states={queued:'等待处理',processing:'正在生成',dispatching:'正在提交回复',succeeded:'已回复',completed:'处理完成',
  uncertain:'结果待核对',deferred:'等待恢复',cancelled:'已取消',failed:'处理失败'};
 const reasons={interval:'等待回复间隔',quota_exhausted:'今日回复额度已用完',model_quota:'模型额度已用完',
  context_changed:'上下文已变化',run_ended:'本次运行已结束',user_cancelled:'已手动取消',reply_cancelled:'原消息已保留',retry_limit:'已达到重试上限',expired:'已过期'};
 async function queue(){
  const view=epoch,generation=++queueRequest;
  try{
   const data=await call('room.queue',{});
   if(view!==epoch||generation!==queueRequest||!el('queue').open)return;
   const rows=data.messages.map(item=>{
    const row=document.createElement('p');
    const files=item.images?'图片 × '+item.images:'';
    const summary=[item.text,files].filter(Boolean).join(' · ');
    row.textContent=[item.at&&new Date(item.at).toLocaleString(),summary,states[item.status]||'待核对',reasons[item.reason]].filter(Boolean).join(' · ');
    if(['queued','processing','deferred'].includes(item.status)){
     const button=document.createElement('button');button.className='secondary';button.textContent='取消本次回复';
     button.disabled=cancelling.has(item.id);
     button.onclick=async()=>{
      if(view!==epoch||cancelling.has(item.id))return;
      cancelling.add(item.id);button.disabled=true;
      try{
       await call('room.cancel',{id:item.id});
       if(view===epoch){el('notice').textContent='已取消本次回复。原消息已保留，可在后续对话中使用。';void history(before);}
      }catch(error){if(view===epoch)el('notice').textContent=error.message;}
      finally{if(view===epoch){cancelling.delete(item.id);if(el('queue').open)void queue();}}
     };
     row.append(button);
    }
    return row;
   });
   if(!rows.length){const empty=document.createElement('p');empty.textContent='暂无已提交的消息。';rows.push(empty);}
   el('jobs').replaceChildren(...rows);
  }catch(error){if(view===epoch&&generation===queueRequest)el('jobs').textContent=error.message;}
 }
 el('queue').ontoggle=()=>{if(el('queue').open)void queue();else{queueRequest++;el('jobs').replaceChildren();}};
 el('text').oninput=()=>{nonce=null;controls();};
 el('pick').onchange=async()=>{
  const view=epoch,files=Array.from(el('pick').files);
  el('pick').value='';
  if(!files.length||selecting||posting||!session)return;
  selecting=true;draft();
  try{
   if(files.length+pictures.length>3)throw Error('每条消息最多 3 张图片。');
   const selected=[];
   for(const file of files){
    if(!file.size||file.size>8*1024*1024)throw Error('每张图片须非空且不超过 8 MiB。');
    const bytes=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
    if(view!==epoch)return;
    const hash=Array.from(new Uint8Array(bytes),b=>b.toString(16).padStart(2,'0')).join('');
    if([...pictures,...selected].some(item=>item.hash===hash))throw Error('同一张图片只需添加一次。');
    selected.push({file,hash});
   }
   pictures.push(...selected);nonce=null;el('notice').textContent='';
  }catch(error){if(view===epoch)el('notice').textContent=error.message;}
  finally{if(view===epoch){selecting=false;draft();}}
 };
 el('send').onclick=async()=>{
  const value=el('text').value;
  if(!session||(!value.trim()&&!pictures.length)||selecting||posting)return;
  const view=epoch;posting=true;draft();
  nonce=nonce||crypto.randomUUID().replaceAll('-','');
  try{
   if(pictures.length)await call('room.post_files',{text:value,nonce,hashes:pictures.map(p=>p.hash)},pictures.map(p=>p.file));
   else await call('room.post',{text:value,nonce});
   if(view!==epoch)return;
   el('text').value='';pictures=[];nonce=null;
   el('notice').textContent='消息已接收。';query='';el('query').value='';void history();if(el('queue').open)void queue();
  }catch(error){if(view===epoch)el('notice').textContent=error.message;}
  finally{if(view===epoch){posting=false;draft();}}
 };
 window.addEventListener('qiban-management-state',({detail})=>{
  const current=detail.ready&&!detail.locked?detail.roomSession:null;
  if(current!==session){close();session=current;}
  el('open').disabled=!session;el('lock').hidden=!detail.lockEnabled;
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management_error'){
   for(const job of pending.values()){clearTimeout(job.timeout);job.reject(Error(data.message));}
   pending.clear();close();return;
  }
  const job=pending.get(data.request_id);
  if(!job||data.type!=='management')return;
  pending.delete(data.request_id);clearTimeout(job.timeout);
  if(data.response.ok)job.resolve(data.response.result);else job.reject(Error(data.response.message));
 });
})();
