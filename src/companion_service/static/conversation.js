"use strict";
(() => {
  if(kind!=="role"||document.body.dataset.authenticated!=="true")return;
  let shown=[], newest="", older="", historical=false, query="", loading=false, revision=0;
  let uploads=[], submitting=false, submission=null;
  const labels={queued:"等待处理",processing:"正在思考",deferred:"等待恢复条件",cancelled:"已取消",failed:"处理失败",uncertain:"结果待核对",succeeded:"已回复",completed:"处理完成"};
  const reasons={quota_exhausted:"今日回复额度已用完",model_quota_exhausted:"今日模型额度已用完",model_budget:"今日模型额度已用完",context_changed:"资料已变化，等待重新处理",user_cancelled:"你取消了这条消息",session_or_run_ended:"本次会话或运行已结束"};
  function render(){
    const area=$('#messages'), nearBottom=area.scrollHeight-area.scrollTop-area.clientHeight<100, position=area.scrollTop;
    area.replaceChildren();
    if(!shown.length){const box=document.createElement('div');box.className='empty';if(!query){const img=document.createElement('img');img.className='empty-mascot';img.src='/static/white-dragon-tea.png';img.alt='白龙娘为你备好热茶';img.width=180;img.height=180;box.append(img);}const p=document.createElement('p');p.textContent=query?'本页没有找到匹配内容；可继续搜索更早记录。':'茶已经泡好了，想聊些什么？';box.append(p);area.append(box);}
    for(const item of shown){const bubble=document.createElement('div');bubble.className='bubble '+item.speaker;
      const label=document.createElement('span');label.className='by';label.textContent=(item.speaker==='user'?'你':'角色')+' · '+new Date(item.at).toLocaleString();
      bubble.append(label,document.createTextNode(item.text));
      for(const attachment of item.attachments||[]){const match=/^input-image:([a-f0-9]{32})$/.exec(attachment.url||'');if(!match)continue;const img=document.createElement('img');img.src='/input-image?id='+match[1];img.alt='本条消息的图片';img.loading='lazy';bubble.append(img);}
      area.append(bubble);
    }
    $('#history-older').hidden=!older;$('#history-latest').hidden=!historical;
    if(!historical&&nearBottom)area.scrollTop=area.scrollHeight;else area.scrollTop=position;
  }
  async function loadPage(options={}, replace=true){
    const turn=++revision;
    const page=await api('/conversation',{...options,query,known:shown.map(item=>item.id),known_versions:Object.fromEntries(shown.map(item=>[item.id,item.version]))});
    if(turn!==revision)return;
    if(replace)shown=page.messages;
    else{const combined=new Map(shown.filter(item=>!page.removed.includes(item.id)).map(item=>[item.id,item]));for(const item of [...page.updates,...page.messages])combined.set(item.id,item);shown=[...combined.values()].slice(-100);}
    newest=page.newest||newest;
    if(replace)older=page.next;else if(shown.length)older=shown[0].cursor;
    render();
  }
  async function queue(){const rows=await api('/conversation');const area=$('#message-queue');area.replaceChildren();for(const row of rows.filter(item=>!['completed','succeeded'].includes(item.status))){const line=document.createElement('div');line.className='queue-item';const text=document.createElement('span');text.textContent=(row.text||'消息')+' · '+(labels[row.status]||'待核对')+(row.reason?'：'+(reasons[row.reason]||'请查看管理室状态'):'');line.append(text);if(['queued','processing','deferred','failed'].includes(row.status)){const cancel=document.createElement('button');cancel.className='quiet';cancel.textContent='取消';cancel.addEventListener('click',async()=>{cancel.disabled=true;try{await api('/cancel-message',{id:row.id});await window.refreshConversation();}catch(e){notice(e.message);}finally{cancel.disabled=false;}});line.append(cancel);}area.append(line);}}
  window.refreshConversation=async()=>{if(loading)return;loading=true;try{if(!historical)await loadPage(newest?{after:newest}:{},!newest);else{const page=await api('/conversation',{after:newest,known:shown.map(item=>item.id),known_versions:Object.fromEntries(shown.map(item=>[item.id,item.version])),limit:1});if(page.removed.length||page.updates.length){const changed=new Map(page.updates.map(item=>[item.id,item]));shown=shown.filter(item=>!page.removed.includes(item.id)).map(item=>changed.get(item.id)||item);render();}}await queue();}catch(e){shown=[];$('#message-queue').replaceChildren();render();throw e;}finally{loading=false;}};
  bind('#history-search',async()=>{query=$('#history-query').value.trim();historical=!!query;newest='';await loadPage();},'submit');
  bind('#history-older',async()=>{historical=true;await loadPage({before:older});});
  bind('#history-latest',async()=>{query='';$('#history-query').value='';historical=false;newest='';await loadPage();});
  function renderUploads(){const list=$('#message-uploads');list.replaceChildren();for(const item of uploads){const box=document.createElement('div'),img=document.createElement('img'),remove=document.createElement('button');img.src='/input-image?id='+item.id;img.alt='待发送图片';remove.type='button';remove.className='quiet';remove.textContent='移除';remove.disabled=submitting;remove.addEventListener('click',async()=>{try{const response=await fetch('/input-image?id='+item.id,{method:'DELETE',headers:{'X-CSRF-Token':csrf}});if(!response.ok)throw new Error('图片已发送或会话失效，请刷新核对');uploads=uploads.filter(value=>value.id!==item.id);renderUploads();}catch(e){notice(e.message);}});box.append(img,remove);list.append(box);}}
  bind('#message-file',async()=>{const input=$('#message-file');for(const file of input.files){if(uploads.length>=3)throw new Error('每条消息最多 3 张图片');if(file.size>8*1024*1024)throw new Error('每张图片不能超过 8 MB');const response=await fetch('/input-image',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':csrf},body:file});const value=await response.json();if(!response.ok)throw new Error(value.message||'图片上传失败');uploads.push(value);renderUploads();}input.value='';},'change');
  bind('#message',async()=>{if(submitting)return;submitting=true;$('#message-file').disabled=true;renderUploads();try{const body={text:$('#message-text').value,images:uploads.map(item=>item.id)};const signature=JSON.stringify(body);if(!submission||submission.signature!==signature)submission={signature,nonce:nonce()};await api('/messages',{...body,nonce:submission.nonce});submission=null;uploads=[];$('#message-text').value='';historical=false;query='';$('#history-query').value='';newest='';await loadPage();notice('消息已收到，可在下方查看处理状态。');await queue();}finally{submitting=false;$('#message-file').disabled=false;renderUploads();}},'submit');
  window.refreshConversation().catch(e=>notice(e.message));
})();
