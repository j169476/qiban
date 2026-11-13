// Interpersonal claim adjudication page. A claim (subject/object/relation/declarer/
// evidence) is not a "same person" account link and not a platform friendship state:
// this page only changes the status of a claim that already exists, and every change is
// preview -> confirm with a server-side token.
//
// What it must never do, and therefore never renders or claims: creating a relation,
// rewriting the statement, subject, declarer or original evidence, re-pinning sources,
// restoring an old dependent version, or touching account links, reply authorizations or
// any other record permission. Evidence text is shown only when the pinned version still
// equals the current one; a changed/deleted/unsupported item is named as such and its new
// text is never substituted for the old evidence.
//
// The primary flow names people (registration labels), the relation they claimed, the
// original evidence, the chosen status, the reason, the impact and the history. Opaque
// keys, classification codes and revision strings stay in a collapsed diagnostics block.
//
// Asynchronous discipline follows the sharing page: every reply is checked against the
// page epoch and against the identity of the operation that may still touch state before
// *any* assignment, so a reply from before a lock, a failure or a newer read can neither
// repaint the page nor leave an old draft or preview operable. A fresh read of the list
// (unlock, refresh, status filter) retires the old detail/draft/preview even when the same
// claim is still listed, and nothing can navigate or re-read while a commit is in flight.
(()=>{
 'use strict';
 const el=id=>document.getElementById('relation-claims-'+id),pending=new Map();
 const STATUS={claimed:'仅声明（尚未核对）',confirmed:'已确认',disputed:'有争议',revoked:'已撤销'};
 const LIST_LIMIT=20,HISTORY_LIMIT=20,REASON_MAX=1000;
 // The filter is always sent explicitly; anything unexpected falls back to "all"
 // rather than sending an unknown status the server would refuse.
 const FILTERS=['all','claimed','confirmed','disputed','revoked'];
 const TIMEOUT='操作超时，请重新读取人际关系声明。';
 let allowed=false,epoch=0,stale=false;
 let list=null,listSeq=0,listActive=0;
 let selected=null,claim=null,detailSeq=0,detailActive=0;
 let history=null,historySeq=0,historyActive=0;
 let preview=null,previewSeq=0,previewActive=0,committing=false;
 // Typed but not yet submitted values survive a re-render; they are dropped whenever the
 // selected claim, the unlock state or a full re-read changes.
 let draft={status:'',reason:''};
 const notes=new Map();
 const listBusy=()=>listActive!==0&&listActive===listSeq;
 const detailBusy=()=>detailActive!==0&&detailActive===detailSeq;
 const historyBusy=()=>historyActive!==0&&historyActive===historySeq;
 const previewBusy=()=>previewActive!==0&&previewActive===previewSeq;
 const busy=()=>listBusy()||detailBusy()||historyBusy()||previewBusy();
 // Anything that would change what the administrator is looking at is refused while a
 // commit may still be writing, so no answer can contradict the submitted decision.
 const navigating=()=>committing||previewBusy();
 function statusText(value){
  if(typeof value!=='string'||!value)return '状态未记录';
  return Object.prototype.hasOwnProperty.call(STATUS,value)?STATUS[value]:textValue(value);
 }
 // Every value is rendered as text; markup is never assigned anywhere on this page.
 function textValue(value){
  if(value===null||value===undefined)return '无';
  if(typeof value==='boolean')return value?'是':'否';
  if(typeof value==='number')return Number.isFinite(value)?String(value):'不是有效数字';
  if(typeof value==='string')return value;
  try{const text=JSON.stringify(value);return typeof text==='string'?text:'无法显示的内容';}
  catch(error){return '无法显示的内容';}
 }
 function timeText(seconds){
  if(seconds===null||seconds===undefined)return '未记录';
  if(typeof seconds!=='number'||!Number.isFinite(seconds))return '时间不可读';
  const date=new Date(seconds*1000);
  return Number.isNaN(date.getTime())?'时间不可读':date.toLocaleString('zh-CN',{hour12:false});
 }
 function accountText(account){
  if(!account||typeof account!=='object')return '未记录';
  if(typeof account.label==='string'&&account.label)return account.label;
  return '未登记账号';
 }
 function listOf(value,key){
  const holder=value&&typeof value==='object'?value[key]:null;
  return Array.isArray(holder)?holder:[];
 }
 function say(place,text){if(typeof text==='string'&&text)notes.set(place,text);else notes.delete(place);}
 function note(place){return notes.get(place)||'';}
 function forget(...keys){for(const key of keys)notes.delete(key);}
 function feedback(parent,place){
  const node=document.createElement('span');
  node.className='relation-claims-feedback';node.setAttribute('role','status');
  node.textContent=note(place)||'';parent.append(node);return node;
 }
 function paragraph(parent,text,className){
  const node=document.createElement('p');if(className)node.className=className;
  node.textContent=text;parent.append(node);return node;
 }
 function heading(parent,level,text){
  const node=document.createElement(level);node.textContent=text;parent.append(node);return node;
 }
 function lines(parent,rows,className){
  const box=document.createElement('ul');if(className)box.className=className;
  for(const row of rows){const item=document.createElement('li');item.textContent=row;box.append(item);}
  parent.append(box);return box;
 }
 // Identifiers, revision strings and classification codes are only for cross-checking;
 // they never sit in the primary flow.
 function technical(parent,rows){
  const kept=rows.filter(text=>typeof text==='string'&&text);
  if(!kept.length)return null;
  const box=document.createElement('details');box.className='relation-claims-technical';
  const summary=document.createElement('summary');summary.textContent='技术诊断（仅供核对）';
  box.append(summary);lines(box,kept,'note');parent.append(box);return box;
 }
 function button(parent,label,className){
  const node=document.createElement('button');
  if(className)node.className=className;node.textContent=label;parent.append(node);return node;
 }
 function call(operation,args,timeoutMessage){
  const request_id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{
    pending.delete(request_id);
    const error=Error(typeof timeoutMessage==='string'&&timeoutMessage?timeoutMessage:TIMEOUT);
    error.code='timeout';
    reject(error);
   },15000);
   pending.set(request_id,{resolve,reject,timer});
   window.chrome.webview.postMessage({action:'management',operation,arguments:args,request_id});
  });
 }
 function setAllowed(value){
  if(value===allowed)return;
  allowed=value;reset();
 }
 function reset(){
  epoch++;listSeq++;detailSeq++;historySeq++;previewSeq++;
  listActive=0;detailActive=0;historyActive=0;previewActive=0;committing=false;
  stale=false;list=null;selected=null;claim=null;history=null;preview=null;
  draft=blankDraft();notes.clear();render();
 }
 function blankDraft(){return {status:'',reason:''};}
 function clearPreview(){preview=null;previewSeq++;previewActive=0;forget('preview');}
 function clearHistory(){history=null;historySeq++;historyActive=0;forget('history');}
 function dropSelection(message){
  selected=null;claim=null;detailSeq++;detailActive=0;
  clearPreview();clearHistory();draft=blankDraft();
  forget('detail','history','status','reason','preview');
  if(typeof message==='string'&&message)say('detail',message);
 }
 // --- listing ---------------------------------------------------------------
 function renderList(){
  const box=el('items');box.replaceChildren();
  if(!allowed)return;
  if(!list){paragraph(box,'还没有读取人际关系声明。点“读取关系声明”开始；本页只读取已有声明，不会新增关系。','note');return;}
  if(!list.claims.length){
   paragraph(box,list.after?'没有更多声明了。':'没有符合当前筛选的声明。可切换筛选后重新读取。','note');
   return;
  }
  for(const item of list.claims){
   const row=button(box,accountText(item.subject)+' → '+accountText(item.object)+' · '+
    textValue(item.relation)+' · '+statusText(item.status),'quiet');
   row.disabled=!allowed||listBusy()||navigating();
   if(selected&&selected===item.id)row.setAttribute('aria-current','true');
   row.onclick=()=>{void openClaim(item.id);};
  }
  paragraph(box,'这里只显示声明双方、关系描述与状态；只有本机管理员能读取原文证据与变更历史。','note');
 }
 // --- detail and evidence ----------------------------------------------------
 async function openClaim(identifier){
  if(!allowed||navigating()||typeof identifier!=='string'||!identifier||listBusy())return;
  const current=epoch,token=++detailSeq;
  selected=identifier;claim=null;detailActive=token;
  clearPreview();clearHistory();draft=blankDraft();
  forget('detail','history','status','reason','preview');
  say('detail','正在读取这条声明……');
  render();
  await readDetail(current,token);
 }
 async function readDetail(current,token){
  try{
   const value=await call('relations.detail',{identifier:selected},'读取超时，请重新选择这条声明。');
   if(epoch!==current||token!==detailSeq)return;
   claim=value;stale=false;forget('detail');
   // A detail read is authoritative for this row too: committing must not leave
   // the list contradicting the refreshed detail. Keep the selected detail visible
   // even if its new status removes it from the current filter.
   if(list&&value&&value.claim){
    const row=value.claim,status=filterValue();
    list.claims=list.claims.map(item=>item.id===selected?
     {...item,status:row.status,version:row.version}:item)
     .filter(item=>status==='all'||item.status===status);
   }
   if(value&&value.history_available===true)await loadHistory('');
  }catch(error){
   if(epoch!==current||token!==detailSeq)return;
   if(error.code==='desktop_locked'){setAllowed(false);return;}
   claim=null;clearHistory();
   say('detail',(error.message||'这条声明未能读取。')+' 可以重新选择这条声明重试。');
  }finally{
   if(epoch===current&&token===detailSeq){detailActive=0;render();}
  }
 }
 async function reloadClaim(){
  if(!allowed||navigating()||!selected)return;
  const current=epoch,token=++detailSeq;
  detailActive=token;forget('detail');clearPreview();clearHistory();draft=blankDraft();
  render();
  await readDetail(current,token);
 }
 async function loadHistory(after){
  if(!allowed||committing||!selected||historyBusy())return;
  const current=epoch,seq=++historySeq,page=typeof after==='string'?after:'';
  historyActive=seq;
  render();
  try{
   const value=await call('relations.history',{identifier:selected,after:page,limit:HISTORY_LIMIT},
    '读取变更历史超时，请重试。');
   if(epoch!==current||seq!==historySeq)return;
   const rows=value&&Array.isArray(value.items)?value.items:[];
   history={items:page&&history&&Array.isArray(history.items)?history.items.concat(rows):rows,
    next:value&&typeof value.next==='string'&&value.next?value.next:null,
    note:value&&value.history&&typeof value.history.note==='string'?value.history.note:''};
   forget('history');
  }catch(error){
   if(epoch!==current||seq!==historySeq)return;
   if(error.code==='desktop_locked'){setAllowed(false);return;}
   // A failed first read leaves no history at all; a failed "more" read keeps the
   // records already read so the same button can retry the same page.
   if(!history||!page)history=null;
   say('history',(error.message||'变更历史未能读取。')+' 可以点“读取变更历史”重试。');
  }finally{
   if(epoch===current&&seq===historySeq){historyActive=0;render();}
  }
 }
 function evidenceBlock(item,index){
  const box=document.createElement('article');box.className='relation-claims-evidence';
  const head=document.createElement('h5');
  head.textContent='第 '+index+' 项来源 · '+textValue(item&&item.state_label);
  box.append(head);
  if(item&&typeof item.excerpt==='string'&&item.excerpt){
   paragraph(box,'引用版本下的原文摘录：'+item.excerpt,'relation-claims-excerpt');
   if(item.truncated===true)paragraph(box,'摘录已截断（每项最多 2000 字）。','note');
  }else if(item&&item.matched===true){
   paragraph(box,item.page_limited===true
    ?'引用版本与当前一致，但本页摘录总量已达上限，这一项的正文没有展示。'
    :'引用版本与当前一致，但这一项没有可展示的正文。','note');
  }else{
   paragraph(box,'不展示正文：只有引用版本与当前一致时才会给出摘录，不用现在的正文冒充当时的证据。','note');
  }
  technical(box,['来源标识：'+textValue(item&&item.resource),
   '引用时版本：'+textValue(item&&item.pinned_version),
   '当前版本：'+textValue(item&&item.current_version)]);
  return box;
 }
 function sourceLines(source){
  const box=source&&typeof source==='object'?source:{};
  const issues=Array.isArray(box.issues)?box.issues:[];
  if(box.ok===true)return ['来源检查：没有发现来源版本、停用或过期的阻断问题。'];
  const rows=issues.slice(0,20).map(issue=>'来源问题：'+textValue(issue&&issue.detail));
  if(!rows.length)rows.push('来源检查没有通过，但服务端没有给出具体原因，请先检查这条声明的来源。');
  if(box.limited===true||(typeof box.total_issues==='number'&&box.total_issues>issues.length))
   rows.push('来源问题共 '+textValue(box.total_issues)+' 条，只列出前 '+issues.length+' 条。');
  return rows;
 }
 function sourceTechnical(source){
  const issues=source&&Array.isArray(source.issues)?source.issues:[];
  // The resource id and the machine reason are for cross-checking only; when the
  // backend reports no reason code the human detail is repeated instead of inventing one.
  return issues.slice(0,20).map(issue=>{
   const code=issue&&typeof issue.reason==='string'&&issue.reason?issue.reason:textValue(issue&&issue.detail);
   return '来源：'+textValue(issue&&issue.resource)+' — '+code;
  });
 }
 function readableText(readable){
  if(!readable||typeof readable!=='object')return '来源是否允许读取：服务端没有返回核对结果，本页不判断。';
  if(readable.checked!==true)
   return '来源是否允许读取：未核对。'+(typeof readable.note==='string'?readable.note:'');
  if(readable.allowed===true)
   return '来源是否允许读取：按当前来源与有效状态，'+(readable.actor||'声明人')+
    '的回复仍可以把它作为依据。';
  if(readable.allowed===false)
   return '来源是否允许读取：按当前来源与有效状态，'+(readable.actor||'声明人')+
    '的回复不能把它作为依据（状态已撤销、来源已更换或失效，或分类隔离）。';
  return '来源是否允许读取：核对结果未知，本页不判断。';
 }
 function claimFacts(){
  const value=claim.claim||{};
  const rows=['状态：'+statusText(value.status),
   '关系描述：'+textValue(value.relation),
   '声明人：'+accountText(value.declarer),
   '主体：'+accountText(value.subject)+'；对象：'+accountText(value.object)];
  if(typeof value.declared_reason==='string'&&value.declared_reason)
   rows.push('声明人当时填写的原因：'+value.declared_reason);
  if(typeof value.body_error==='string')rows.push(value.body_error);
  rows.push('核心查询：'+(value.queryable===true
   ?'“已确认/有争议/仅声明”的声明仍会带状态返回'
   :'“已撤销”的声明不再由核心关系查询返回'));
  return rows;
 }
 function claimTechnical(){
  const value=claim.claim||{},policy=claim.policy||{};
  const rows=['声明标识：'+textValue(value.id),
   '声明版本：'+textValue(value.version),
   '权限版本：'+textValue(policy.version),
   '分类代码：'+textValue(policy.classification)+'；可见范围：'+textValue(policy.audience),
   '有效：'+textValue(policy.valid)+'；到期：'+timeText(policy.expires_at),
   '归属账号标识：'+(Array.isArray(policy.owners)&&policy.owners.length
    ?policy.owners.join('、'):'未记录'),
   '对话标识：'+textValue(policy.conversation)];
  for(const pin of listOf(policy,'source_versions'))
   rows.push('固定来源：'+textValue(pin&&pin[0])+' @ '+textValue(pin&&pin[1]));
  return rows;
 }
 function impactText(impact){
  if(!impact||typeof impact!=='object')return '依赖影响：服务端没有返回，本页不判断。';
  const total=typeof impact.total_count==='number'?impact.total_count:null;
  const downstream=typeof impact.downstream_count==='number'?impact.downstream_count:null;
  if(total===null)return '依赖影响：数量未记录。';
  return '依赖影响：共 '+total+' 项，其中包含这条声明自身 1 项'+
   (downstream!==null?'、下游 '+downstream+' 项':'')+
   '；这里只统计数量与标识，不推断其余资料能否读取。';
 }
 function anomalyText(){
  const check=claim&&claim.check;
  if(!check||typeof check!=='object')return '';
  if(check.consistent===false)
   return '检查异常：'+(typeof check.detail==='string'?check.detail:'这条声明的状态与权限有效标记不一致')+
    '本页不会默默把它改回有效，也不会提交任何更改；请先由维护机制核对。';
  return '';
 }
 function statusForm(){
  const box=document.createElement('div');box.className='relation-claims-form';
  heading(box,'h4','更正状态');
  const current=claim.claim&&typeof claim.claim.status==='string'?claim.claim.status:'';
  const targets=(Array.isArray(claim.targets)?claim.targets:[])
   .filter(item=>item&&typeof item.value==='string'&&item.value!==current);
  if(!targets.length){
   paragraph(box,'服务端没有给出可选的状态，因此这里不能更正。','note');
   box.append(feedback(box,'status'));
   return box;
  }
  const anomaly=anomalyText();
  if(anomaly)paragraph(box,anomaly,'note');
  if(claim.claim&&claim.claim.restorable===true)
   paragraph(box,'这条声明当前已撤销：改回“已确认”或“有争议”就是恢复此声明。'+
    '已失效或已更换的来源不会因此重新可读，来源版本不会被重新固定，旧依赖版本也不会自动恢复。','note');
  const label=document.createElement('label');label.textContent='新的状态（不会为同状态增加版本）';
  const select=document.createElement('select');
  for(const item of targets){
   const option=document.createElement('option');
   option.value=item.value;
   option.textContent=typeof item.label==='string'&&item.label?item.label:statusText(item.value);
   select.append(option);
  }
  const wanted=targets.some(item=>item.value===draft.status)?draft.status:targets[0].value;
  select.value=wanted;
  select.onchange=()=>{draft.status=select.value;};
  label.append(select);box.append(label);
  box.append(feedback(box,'status'));
  const reasonLabel=document.createElement('label');
  reasonLabel.textContent='更正说明（可选，最多 '+REASON_MAX+' 字；只写入变更历史，不改声明正文）';
  const reason=document.createElement('textarea');
  reason.rows=3;reason.maxLength=REASON_MAX;reason.value=draft.reason;
  reason.oninput=()=>{draft.reason=reason.value;};
  reasonLabel.append(reason);box.append(reasonLabel);
  box.append(feedback(box,'reason'));
  const actions=document.createElement('div');actions.className='actions';
  const run=button(actions,'预览这次状态更正');
  run.disabled=!allowed||busy()||stale||Boolean(anomaly)||!claim.claim;
  run.onclick=()=>{
   // Defence in depth: a blocked form submits nothing, whichever way the handler
   // was reached (for example a forced click on the disabled button).
   if(!allowed||busy()||stale||Boolean(anomaly)||!claim.claim)return;
   draft.status=select.value;draft.reason=reason.value;
   forget('status','reason','preview-action');
   if(draft.status===current){
    say('status','新状态与当前状态相同，没有需要提交的更改；本页不会为同状态增加版本。');
    say('preview-action','这次没有需要提交的更改。');render();return;
   }
   if(draft.reason.length>REASON_MAX){
    say('reason','更正说明最多 '+REASON_MAX+' 字，请缩短后再预览。');
    say('preview-action','请先修正上方标出的项目。');render();return;
   }
   void previewChange(draft.status,draft.reason);
  };
  box.append(actions);box.append(feedback(box,'preview-action'));
  return box;
 }
 function historyLines(item){
  if(item&&item.unreadable===true)return [textValue(item&&item.detail)];
  const before=item&&item.before?item.before:{},after=item&&item.after?item.after:{};
  const rows=[timeText(item&&item.at)+' · '+(typeof item.actor_label==='string'?item.actor_label:'未登记管理员')+
   '：'+statusText(before.status)+' → '+statusText(after.status)];
  if(item&&item.restored===true)rows.push('这一次记录恢复了已撤销的声明。');
  const reason=item&&typeof item.reason==='string'?item.reason:'';
  rows.push('更正说明：'+(reason||'未填写'));
  return rows;
 }
 function historyTechnical(item){
  if(!item||item.unreadable===true)return ['记录位置：'+textValue(item&&item.cursor)];
  const before=item.before?item.before:{},after=item.after?item.after:{};
  return ['记录位置：'+textValue(item.cursor),
   '声明版本：'+textValue(before.version)+' → '+textValue(after.version),
   '权限版本：'+textValue(before.policy_version)+' → '+textValue(after.policy_version),
   '管理员标识：'+textValue(item.actor)];
 }
 function renderHistory(){
  const box=el('history');box.replaceChildren();
  if(!allowed||!claim)return;
  const info=claim.history&&typeof claim.history==='object'?claim.history:{};
  heading(box,'h4','变更历史');
  if(typeof info.note==='string'&&info.note)paragraph(box,info.note,'note');
  if(claim.history_available!==true&&(!history||!history.items.length)){
   paragraph(box,'这条声明当前没有本页记录的裁决。','note');
   box.append(feedback(box,'history'));
   return;
  }
  const items=history&&Array.isArray(history.items)?history.items:[];
  if(!items.length)paragraph(box,history?'还没有读取到变更历史。':'正在读取变更历史……','note');
  for(const item of items){
   const entry=document.createElement('article');entry.className='relation-claims-history';
   for(const line of historyLines(item))paragraph(entry,line,'note');
   technical(entry,historyTechnical(item));
   box.append(entry);
  }
  const actions=document.createElement('div');actions.className='actions';
  const more=button(actions,'读取更多变更历史','secondary');
  more.disabled=!allowed||busy()||!history||!history.next;
  more.onclick=()=>{const next=history&&history.next;if(typeof next==='string'&&next)void loadHistory(next);};
  const reload=button(actions,'重新读取变更历史','secondary');
  reload.disabled=!allowed||busy()||!selected;
  reload.onclick=()=>{void loadHistory('');};
  paragraph(actions,'历史按写入顺序列出，每条不可变，不会自动删除；不包含本功能开始记录之前的裁决。','note');
  actions.append(feedback(actions,'history'));
  box.append(actions);
 }
 function renderDetail(){
  const box=el('detail');box.replaceChildren();
  if(!allowed)return;
  if(!selected){
   paragraph(box,note('detail')||'先在上方选择一条声明，这里会显示它的状态、证据、来源与依赖影响。','note');
   return;
  }
  if(!claim){paragraph(box,note('detail')||'正在读取这条声明……','note');return;}
  heading(box,'h3',textValue(claim.claim&&claim.claim.relation)+' · '+statusText(claim.claim&&claim.claim.status));
  lines(box,claimFacts());
  const anomaly=anomalyText();
  if(anomaly)paragraph(box,anomaly,'note');
  heading(box,'h4','原证据（最多 16 项，只有引用版本与当前一致时才给出摘录）');
  const evidence=claim.evidence&&Array.isArray(claim.evidence.items)?claim.evidence.items:[];
  if(!evidence.length)paragraph(box,'这条声明没有登记任何引用来源。','note');
  evidence.forEach((item,index)=>box.append(evidenceBlock(item,index+1)));
  if(claim.evidence&&claim.evidence.declared_count>evidence.length)
   paragraph(box,'声明登记的证据超过本页上限，只显示前 '+evidence.length+' 项。','note');
  heading(box,'h4','来源检查');
  lines(box,sourceLines(claim.source),'note');
  paragraph(box,readableText(claim.readable),'note');
  paragraph(box,impactText(claim.impact),'note');
  technical(box,[...claimTechnical(),...sourceTechnical(claim.source),'来源状态摘要：'+
   textValue(claim.source&&claim.source.digest)]);
  box.append(statusForm());
  if(typeof claim.meaning==='string'&&claim.meaning)paragraph(box,claim.meaning,'note');
 }
 // --- preview and commit -----------------------------------------------------
 function previewLines(){
  const before=preview.before||{},after=preview.after||{};
  const rows=['当前状态：'+statusText(before.status),
   '提交后：'+statusText(after.status)+'（权限有效 '+textValue(after.valid)+'）'];
  if(preview.restored===true||after.restores===true)
   rows.push('这一次是恢复此声明：已撤销改回可用状态；来源不被重新固定，旧依赖版本不自动恢复。');
  rows.push('更正说明：'+(typeof preview.reason==='string'&&preview.reason?preview.reason:'未填写')+
   '（只写入变更历史）');
  rows.push('保持不变：正文、主体、对象、声明人、原证据、归属账号与到期时间；'+
   '账号关联、回复授权和其他资料权限也不变。');
  return rows;
 }
 function renderPreview(){
  const box=el('preview'),wrap=el('preview-area');
  box.replaceChildren();
  const message=note('preview');
  if(!allowed||(!preview&&!message)){wrap.hidden=true;return;}
  wrap.hidden=false;
  if(!preview){paragraph(box,message,'note');return;}
  heading(box,'h4','确认这次状态更正');
  paragraph(box,'这一步还没有写入任何更改：只有点“确认提交”才会提交这次预览的服务端口令。','note');
  lines(box,previewLines());
  heading(box,'h5','来源检查');
  lines(box,sourceLines(preview.source),'note');
  paragraph(box,impactText(preview.impact),'note');
  if(typeof preview.notice==='string'&&preview.notice)paragraph(box,preview.notice,'note');
  if(preview.readable_before&&typeof preview.readable_before.note==='string')
   paragraph(box,'更正前的来源核对：'+preview.readable_before.note,'note');
  technical(box,[
   '声明版本：'+textValue(preview.before&&preview.before.version)+' → 提交后新增',
   '权限版本：'+textValue(preview.before&&preview.before.policy_version),
   ...sourceTechnical(preview.source),
   '来源状态摘要：'+textValue(preview.source&&preview.source.digest)]);
  const actions=document.createElement('div');actions.className='actions';
  const confirm=button(actions,'确认提交');
  confirm.disabled=!allowed||busy()||stale;
  confirm.onclick=()=>{void commitPreview();};
  const cancel=button(actions,'取消这次预览','secondary');
  cancel.disabled=!allowed||previewBusy()||listBusy();
  cancel.onclick=()=>{void cancelPreview();};
  box.append(actions);box.append(feedback(box,'preview'));
 }
 async function previewChange(status,reason){
  if(!allowed||navigating()||stale||!selected||!claim)return undefined;
  const current=epoch,op=++previewSeq;
  previewActive=op;preview=null;
  say('preview','正在核对这次状态更正的影响……');
  render();
  try{
   const value=await call('relations.preview',
    {identifier:selected,status,reason},'预览超时，请重新核对后再预览。');
   if(epoch!==current||op!==previewSeq)return undefined;
   preview=value;forget('preview','preview-action');
  }catch(error){
   if(epoch!==current||op!==previewSeq)return undefined;
   if(error.code==='desktop_locked'){setAllowed(false);return undefined;}
   say('preview',(error.message||'预览没有完成。')+' 可以重新核对后再预览。');
  }finally{
   if(epoch===current&&op===previewSeq){previewActive=0;render();}
  }
  return undefined;
 }
 function commitText(value){
  const rows=['状态更改成功：'+statusText(value.before&&value.before.status)+' → '+
   statusText(value.after&&value.after.status)+'，并已写入一条不可变的裁决记录。'];
  if(value.restored===true)
   rows.push('这次是恢复已撤销的声明：来源没有被重新固定，旧依赖版本也没有自动恢复。');
  // "The status change succeeded" and "the sources still allow a read" are separate
  // answers: a source that was already invalidated does not become readable again.
  rows.push(value.readable_notice||readableText(value.readable));
  rows.push('账号关联、回复授权与其他资料权限未改变；详情与历史会在重新读取后刷新。');
  return rows.join(' ');
 }
 async function commitPreview(){
  if(!allowed||stale||committing||previewBusy()||listBusy()||!preview)return;
  const current=epoch,op=++previewSeq,value=preview;
  previewActive=op;committing=true;forget('preview');
  say('preview','正在提交，服务端会重新核对声明、权限、来源与依赖……');
  render();
  let outcome=null,refresh=false;
  try{
   outcome=await call('relations.commit',{token:value.token},
    '提交超时：写入结果未知，这次没有自动重试；请重新读取这条声明确认当前状态。');
   if(epoch!==current||op!==previewSeq)return;
   preview=null;forget('preview');
  }catch(error){
   if(epoch!==current||op!==previewSeq)return;
   preview=null;forget('preview');
   if(error.code==='desktop_locked'){setAllowed(false);return;}
   refresh=true;
   if(error.code==='timeout'){
    stale=true;say('panel',error.message);
   }else if(error.code==='edit_conflict'||error.code==='relation_preview_expired'
    ||error.code==='request_id_conflict'||error.code==='relation_inconsistent'){
    stale=true;
    say('panel',(error.message||'这次预览已失效。')+' 已标记为需要重新读取，不会自动重新提交。');
   }else say('panel',(error.message||'提交没有完成。')+' 没有自动重试；请重新读取声明后重试。');
  }finally{
   if(epoch===current&&op===previewSeq){previewActive=0;committing=false;render();}
  }
  if(epoch!==current||op!==previewSeq)return;
  if(outcome){
   say('panel',commitText(outcome));
   refresh=true;
  }
  if(refresh)await reloadClaim();
 }
 async function cancelPreview(){
  if(!allowed||!preview||previewBusy()||listBusy())return;
  const current=epoch,value=preview;
  // Clearing first makes a second click a no-op, so a cancel can never be sent twice.
  preview=null;previewSeq++;previewActive=0;
  forget('preview');
  say('panel','已取消这次预览，没有写入任何更改。');
  render();
  try{
   await call('relations.cancel',{token:value.token},
    '取消请求超时；服务端预览会自行过期，本页不会再提交这次预览。');
  }catch(error){
   // A late rejection belongs to the page as it was: after a newer read, a new
   // unlock or any other epoch change it must not lock a page the user reopened.
   if(epoch!==current)return;
   if(error.code==='desktop_locked')setAllowed(false);
  }
 }
 // --- listing reads and wiring -----------------------------------------------
 function filterValue(){
  const node=el('filter');
  const value=node&&typeof node.value==='string'?node.value:'';
  return FILTERS.indexOf(value)>=0?value:'all';
 }
 async function loadPage(after){
  if(!allowed||committing||listBusy())return;
  const current=epoch,seq=++listSeq,page=typeof after==='string'?after:'',status=filterValue();
  listActive=seq;
  if(page===''){
   // A fresh read (refresh, filter, unlock) retires whatever was typed or previewed
   // before: no draft may stay operable against a list that was just re-read.
   clearPreview();draft=blankDraft();forget('status','reason','preview-action');
  }
  say('panel',page?'正在读取下一页声明……':'正在读取人际关系声明……');
  render();
  try{
   const value=await call('relations.list',{after:page,limit:LIST_LIMIT,status},
    '读取声明超时，请重新读取人际关系声明。');
   // Nothing is assigned before this check: a reply from before a lock, a failure
   // or a newer read must not revive or supersede the page.
   if(epoch!==current||seq!==listSeq)return;
   const rows=value&&Array.isArray(value.claims)?value.claims:[];
   const kept=page&&list?list.claims:[];
   list={claims:kept.concat(rows),next:typeof value.next==='string'&&value.next?value.next:null,
    after:typeof value.after==='string'?value.after:''};
   say('panel','已读取 '+list.claims.length+' 条声明。');
   if(selected&&!list.claims.some(item=>item&&item.id===selected))
    dropSelection('刚才选中的声明已不在这次读取的结果里，已取消选择；需要时请重新选择。');
   else if(selected&&page==='')
    // The same claim is still listed, but the list was re-read: refresh the detail so
    // nothing shown (and nothing typed) describes an older state of it.
    await reloadClaim();
  }catch(error){
   if(epoch!==current||seq!==listSeq)return;
   if(error.code==='desktop_locked'){setAllowed(false);return;}
   // A failed read leaves nothing editable behind: no old list, detail, draft or preview.
   list=null;dropSelection('');
   say('panel',(error.message||'声明未能读取。')+' 可以点“读取关系声明”重试。');
  }finally{
   if(epoch===current&&seq===listSeq){listActive=0;render();}
  }
 }
 function render(){
  el('load').disabled=!allowed||listBusy()||navigating();
  el('filter').disabled=!allowed||listBusy()||navigating();
  el('status').textContent=note('panel')||'';
  renderList();
  el('next').disabled=!allowed||listBusy()||navigating()||!list||!list.next;
  renderDetail();
  renderHistory();
  renderPreview();
 }
 if(el('load'))el('load').onclick=()=>{void loadPage('');};
 if(el('filter'))el('filter').onchange=()=>{if(allowed&&!navigating()&&!listBusy())void loadPage('');};
 if(el('next'))el('next').onclick=()=>{
  const next=list&&list.next;
  if(allowed&&!navigating()&&typeof next==='string'&&next)void loadPage(next);
 };
 window.addEventListener('qiban-management-state',({detail})=>{
  el('panel').hidden=document.getElementById('management-panel').hidden;
  const now=Boolean(detail&&detail.ready&&!detail.locked);
  if(now===allowed)return;
  setAllowed(now);
  if(allowed)void loadPage('');
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  const job=data&&data.request_id?pending.get(data.request_id):null;
  if(data&&data.type==='management'&&job){
   clearTimeout(job.timer);pending.delete(data.request_id);
   if(data.response.ok)job.resolve(data.response.result);
   else{
    const error=Error(data.response.message);
    error.code=data.response.error||'';
    job.reject(error);
   }
  }else if(data&&data.type==='management_error'){
   for(const job of pending.values()){
    clearTimeout(job.timer);
    const error=Error(data.message);
    error.code=data.code||'';
    job.reject(error);
   }
   pending.clear();
  }
 });
 render();
})();
