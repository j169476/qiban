"use strict";
(() => {
  if(kind!=="admin")return;
  const resetTicket=location.hash.startsWith('#reset=')?location.hash.slice(7):'';
  if(resetTicket){history.replaceState(null,'','/');const form=document.createElement('form');form.className='card';const title=document.createElement('h2');title.textContent='重设本机网页密码';const label=document.createElement('label');label.textContent='新密码';const input=document.createElement('input');input.type='password';input.minLength=12;input.maxLength=256;input.required=true;input.autocomplete='new-password';const button=document.createElement('button');button.type='submit';button.textContent='设置新密码并退出所有会话';label.append(input);form.append(title,label,button);$('main').prepend(form);form.addEventListener('submit',async e=>{e.preventDefault();button.disabled=true;try{await api('/password-reset',{ticket:resetTicket,password:input.value});location.reload();}catch(err){notice(err.message);button.disabled=false;}});}
  if(document.body.dataset.authenticated!=="true")return;
  let replyRequest=0,replyOffset=0,replyNext=null,replySelection=null;
  function clearReplyResults(){
    replyRequest++;replySelection=null;replyOffset=0;replyNext=null;
    $('#reply-authorizations').replaceChildren();$('#reply-pagination').hidden=true;
    $('#reply-result-summary').textContent='输入完整号码或 ID 后检索，或查看该平台已授权对象。';
  }
  async function replyAuthorizations(offset=0){
    if(!replySelection)return;
    const request=++replyRequest,selection={...replySelection};
    $('#reply-authorizations').replaceChildren();$('#reply-pagination').hidden=true;
    $('#reply-result-summary').textContent='正在查找…';
    let data;
    try{data=await operate('reply_authorization_list',{...selection,offset,limit:20});}
    catch(error){if(request===replyRequest)$('#reply-result-summary').textContent='读取失败，请重试。';throw error;}
    if(request!==replyRequest)return;
    replyOffset=data.offset??offset;replyNext=data.next_offset??null;
    const platforms={wechat:'微信',qq:'QQ'};
    $('#reply-platforms').textContent=Object.entries(data.platforms).map(([key,value])=>`${platforms[key]||key}：${value.available?'连接器已接入':'连接器未接入，暂不可新增授权'}${value.discovery_failed?'；部分身份核验失败，请检查连接器':''}`).join('；');
    const list=$('#reply-authorizations');list.replaceChildren();
    const total=data.total??data.items.length;
    $('#reply-result-summary').textContent=`${selection.view==='authorized'?'已授权对象':'检索结果'}：共 ${total} 项。检索不会授予回复权限。`;
    $('#reply-pagination').hidden=total===0;
    $('#reply-previous').disabled=replyOffset===0;$('#reply-next').disabled=replyNext===null;
    $('#reply-previous').hidden=replyOffset===0;$('#reply-next').hidden=replyNext===null;
    $('#reply-page-label').textContent=total?`${replyOffset+1}–${replyOffset+data.items.length} / ${total}`:'';
    if(!data.items.length){const empty=document.createElement('p');empty.className='soft-empty';empty.textContent=selection.view==='authorized'?'该平台暂无已授权对象。': '没有找到匹配的已核验会话。请检查平台和完整 ID；尚未核验的对象需先完成连接器配置或导入核验。';list.append(empty);}
    for(const item of data.items){
      const card=document.createElement('article');card.className='task-item';
      const title=document.createElement('h3');title.textContent=`${platforms[item.scope.platform]} · ${item.label} · ${item.enabled?'已授权':'未授权'}`;
      const identity=document.createElement('pre');identity.textContent=`机器人账号：${item.account_label}\n账号身份：${item.scope.account_identity}\n会话类型：${item.scope.kind==='group'?'群聊':'私聊'}\n会话内部 ID：${item.scope.target_id}`;
      const hint=document.createElement('p');hint.className='task-hint';hint.textContent=item.restriction;
      card.append(title,identity,hint);
      if(item.verified_target||item.enabled){
        const button=document.createElement('button');button.className='secondary';button.textContent=item.enabled?'撤销回复授权':'授权回复此会话';
        button.addEventListener('click',async()=>{button.disabled=true;try{
          if(request!==replyRequest)return;
          if(!confirm(`${item.enabled?'撤销':'允许'} ${platforms[item.scope.platform]} ${item.scope.kind==='group'?'群聊':'私聊'} ${item.scope.target_id} 的回复授权？\n机器人账号：${item.account_label}\n变更会暂停对应平台，之后需明确启动。`))return;
          const result=await operate('reply_authorization_set',{scope_id:item.id,enabled:!item.enabled,expected_version:item.version});
          notice((item.enabled?'已撤销回复授权。':'已保存回复授权。')+(result.platform_paused?'对应平台已暂停，需在平台控制聊天重新启动。':'本次操作没有启动角色。'));
          if(request===replyRequest)await replyAuthorizations(replyOffset>0&&data.items.length===1&&selection.view==='authorized'?replyOffset-20:replyOffset);
        }catch(error){notice(error.message+'；请重新读取会话与回复授权。');}finally{button.disabled=false;}});
        card.append(button);
      }
      list.append(card);
    }
  }
  bind('#reply-search-form',async()=>{
    const query=$('#reply-search-query').value.trim();
    if(!query){clearReplyResults();notice('请输入完整号码或内部 ID。');return;}
    replySelection={platform:$('#reply-search-platform').value,query,view:'search'};
    await replyAuthorizations();
  },'submit');
  bind('#load-reply-authorizations',async()=>{
    replySelection={platform:$('#reply-search-platform').value,query:'',view:'authorized'};
    await replyAuthorizations();
  });
  bind('#reply-previous',()=>replyOffset>0?replyAuthorizations(Math.max(0,replyOffset-20)):null);
  bind('#reply-next',()=>replyNext!==null?replyAuthorizations(replyNext):null);
  bind('#reply-search-platform',clearReplyResults,'change');
  bind('#reply-search-query',clearReplyResults,'input');
  async function replyPolicies(){
    const data=await operate('reply_policy_list',{}),list=$('#reply-policies');list.replaceChildren();
    const labels={private_daily_replies:'每日私聊成功回复上限',group_session_replies:'每次群聊参与回复上限',group_session_decisions:'每次群聊参与判断上限',group_session_minutes:'每次群聊参与最长分钟数'};
    for(const item of data.items){
      const form=document.createElement('form');form.className='task-item';
      const title=document.createElement('h3');title.textContent=`${item.platform==='qq'?'QQ':'微信'} · ${item.account_label}`;
      const used=document.createElement('p');used.className='task-hint';used.textContent=`今日私聊：成功 ${item.private_usage.confirmed}，处理中 ${item.private_usage.reserved}，不确定 ${item.private_usage.uncertain}。额度不因改配置或重启清零。`;
      const fields=document.createElement('div');fields.className='quota-fields';const inputs={};
      for(const [key,label] of Object.entries(labels)){const wrapper=document.createElement('label');wrapper.textContent=label;const input=document.createElement('input');input.type='number';input.required=true;input.step='1';[input.min,input.max]=data.bounds[key];input.value=item.settings[key];inputs[key]=input;wrapper.append(input);fields.append(wrapper);}
      const button=document.createElement('button');button.type='submit';button.textContent='保存并暂停对应平台';form.append(title,used,fields,button);
      form.addEventListener('submit',async e=>{e.preventDefault();button.disabled=true;try{
        await operate('reply_policy_set',{platform:item.platform,account_identity:item.account_identity,expected_version:item.version,changes:Object.fromEntries(Object.entries(inputs).map(([key,input])=>[key,Number(input.value)]))});
        notice('参与设置已保存；未启动角色。请在对应平台控制聊天明确启动。');await replyPolicies();
      }catch(error){notice(error.message+'；请重新读取设置。');}finally{button.disabled=false;}});list.append(form);
    }
    if(!data.items.length){const empty=document.createElement('p');empty.textContent='暂无已核验的QQ或微信账号，请先完成只读身份核验。';list.append(empty);}
  }
  bind('#load-reply-policies',replyPolicies);
  bind('#load-traces',async()=>{const rows=await operate('reply_traces',{limit:30});$('#trace-list').replaceChildren();for(const row of rows){const button=document.createElement('button');button.className='quiet';button.textContent=new Date(row.started).toLocaleString()+' · '+row.platform+' · '+row.status+' · '+(row.duration_ms??'?')+' ms';button.addEventListener('click',async()=>{try{const detail=await operate('reply_trace',{id:row.id});$('#trace-detail').textContent=detail.meaning+'\n'+JSON.stringify(detail,null,2);}catch(e){notice(e.message);}});$('#trace-list').append(button);}});
  let deletion=null;
  function selection(){if(!selected?.value)throw new Error('请先在资料工作台选择一条资料');return selected;}
  let factPage=null,factTarget=null;
  async function showFacts(after=''){
    const target=selection();if(target.kind!=='memories')throw new Error('请先选择一条个人事实记忆');
    if(after&&target!==factTarget)throw new Error('选择已变化，请重新读取第一页');
    const at=$('#fact-at').value;const data=await operate('personal_fact_history',{id:target.id,after,limit:20,...(at?{at:new Date(at).toISOString()}:{})});
    factPage=data;factTarget=target;$('#fact-counts').textContent=`共 ${data.total} 条：当前 ${data.counts.active}，冲突 ${data.counts.disputed}，历史 ${data.counts.historical}，未来 ${data.counts.future}。确认仅替代本页同一属性的当前记录，未显示的冲突仍保留。`;
    $('#next-facts').hidden=!data.next;$('#fact-timeline').replaceChildren();
    const labels={active:'当前',disputed:'冲突待确认',historical:'历史',future:'未来'},attrs={residence:'住址',preferred_name:'称呼',drink_preference:'饮品偏好',food_preference:'饮食偏好'};
    for(const item of data.items){const card=document.createElement('article');card.className='task-item';const title=document.createElement('h3');title.textContent=`${attrs[item.attribute]||item.attribute} · ${item.value} · ${labels[item.status]}`;const quote=document.createElement('blockquote');quote.textContent=item.quote;const meta=document.createElement('p');meta.className='task-hint';meta.textContent=`生效于 ${new Date(item.valid_from).toLocaleString()} · 来源 ${item.source.reference}`+(item.confirmation?' · 管理确认：'+item.confirmation.reason:'');card.append(title,quote,meta);
      if(!at&&['active','disputed'].includes(item.status)){const button=document.createElement('button');button.className='secondary';button.textContent='确认此项并替代本页同类当前记录';button.addEventListener('click',async()=>{button.disabled=true;try{if(selection()!==factTarget)throw new Error('选择已变化，请重新读取');const reason=$('#fact-reason').value.trim();if(!reason)throw new Error('请填写确认原因');await operate('personal_fact_confirm',{id:item.id,revision:data.revision,reason,supersedes:data.items.filter(row=>row.attribute===item.attribute&&['active','disputed'].includes(row.status)).map(row=>row.id)});notice('确认已保存，旧记录及来源保留。');await showFacts();}catch(error){notice(error.message);}finally{button.disabled=false;}});card.append(button);}$('#fact-timeline').append(card);}
  }
  bind('#load-facts',()=>showFacts());bind('#next-facts',()=>showFacts(factPage?.next||''));
  bind('#change-password',async()=>{await api('/password',{old_password:$('#old-password').value,password:$('#new-password').value});location.reload();},'submit');
  bind('#explain-record',async()=>{const value=selection();const explanation=await operate('record_explain',{kind:value.kind,id:value.id});const reasons={revoked:'已撤销',secret:'机密内容禁止进入模型',unclassified:'尚未审核',expired:'已过期',source_missing_or_changed:'来源已删除或修改',incomplete_source_versions:'来源版本不完整'};$('#record-explanation').textContent=explanation.meaning+'\n\n来源图共 '+explanation.total_nodes+' 项，检查发现 '+explanation.total_issues+' 项阻断原因。'+(explanation.limited?'以下仅展示前 200 项。':'')+'\n'+explanation.issues.map(item=>item.resource+'：'+(reasons[item.reason]||item.reason)+(item.source?' → '+item.source:'')).join('\n')+'\n\n'+explanation.nodes.map(item=>item.resource+' · '+item.classification+' · '+(item.valid?'有效':'已撤销')+' · '+item.source_count+' 项直接来源\n  '+item.sources.join('\n  ')).join('\n');});
  bind('#preview-delete',async()=>{const target=selection();deletion={target,...await operate('delete_preview',{kind:target.kind,id:target.id})};$('#delete-impact').textContent=deletion.effect+'\n影响条目：'+deletion.affected_count+'\n'+deletion.sample.join('\n')+(deletion.sample_limited?'\n其余条目未在此展开。':'');$('#confirm-delete').hidden=false;});
  bind('#confirm-delete',async()=>{if(!deletion||selection()!==deletion.target)throw new Error('选择已变化，请重新查看删除预览');if(!confirm(deletion.effect))return;await operate('record_delete',{token:deletion.token});deletion=null;selected=null;$('#editor').value='';$('#record-title').textContent='条目已删除';$('#confirm-delete').hidden=true;notice('当前条目已删除，其来源版本失效；加密历史与既有备份仍保留。');await load();});
  async function choices(){const data=await api('/actors');for(const id of ['#grant-owner','#grant-grantee']){const select=$(id);select.replaceChildren();for(const actor of data.actors){const option=document.createElement('option');option.value=actor.key;option.textContent=actor.platform+' / '+actor.user_id+' · '+actor.key.slice(0,18);select.append(option);}}const grants=await operate('grant_list');$('#grant-list').replaceChildren();for(const grant of grants){const row=document.createElement('div');row.className='queue-item';const text=document.createElement('span');text.textContent=grant.grantee+' ← '+grant.owner+' · '+(grant.revoked?'已撤销':'到期 '+new Date(grant.expires_at*1000).toLocaleString())+' · '+grant.resources;row.append(text);if(!grant.revoked){const button=document.createElement('button');button.className='quiet';button.textContent='撤销';button.addEventListener('click',async()=>{try{await operate('revoke',{grant_id:grant.id});await choices();notice('授权已撤销。');}catch(e){notice(e.message);}});row.append(button);}$('#grant-list').append(row);}}
  bind('#grant-choices',choices);
  bind('#grant-selected',()=>{const item=selection();$('#grant-resources').value=item.kind+'/'+(item.id||'@config');});
  bind('#grant-form',async()=>{await operate('authorize',{owner:$('#grant-owner').value,grantee:$('#grant-grantee').value,resources:$('#grant-resources').value.split('\n').map(s=>s.trim()).filter(Boolean),purposes:['reply'],lifetime:Number($('#grant-days').value)*86400});await choices();notice('已添加指定范围的授权。');},'submit');
})();
