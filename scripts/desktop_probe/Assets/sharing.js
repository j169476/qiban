// Record sharing page. It widens or narrows one record's usable range through the
// existing access rules: a private-chat grant for one registered account, or a
// classification change that keeps the stored owners, sources and expiry. Every
// widening is preview -> confirm with a server-side token; nothing here can
// authorize a conversation, start a platform or change a reply quota.
//
// Asynchronous discipline: every reply is checked against the page epoch and against
// the identity of the operation that may still touch state, before *any* assignment.
// A reply issued before a lock, a backend stop or a newer read therefore cannot
// repaint the page afterwards, and a discarded request never leaves the page stuck
// in a busy state.
(()=>{
 'use strict';
 const el=id=>document.getElementById('sharing-'+id),pending=new Map();
 const KIND={diaries:'日记',knowledge:'知识',memories:'记忆'};
 const PURPOSE={reply:'回复',maintenance:'维护'};
 // "维护" never authorizes a reply: AccessService only accepts the granted purpose.
 const PURPOSE_HINT={reply:'可在私聊回复中使用',maintenance:'只供维护流程使用，不能用于回复'};
 // Shape names come from the backend; the older and the current spellings both map.
 const SHAPE={private:'私聊读取',private_reply:'归属账号私聊',conversation:'原会话',
  public:'公开场合读取',public_read:'公开场合'};
 // Who could still read one record after this grant is revoked. The label names the
 // surviving basis only: whether a read really succeeds is decided by the source check.
 const SCOPE_STATE={public:'分类公开，不依赖这条授权',
  owner:'目标账号是这条资料的归属账号',
  other_grant:'还有另一条有效授权',
  conversation:'仅原会话按分类可读',
  blocked:'资料已停用、过期或隔离，本来就不可读',
  missing:'权限记录缺失',
  none:'撤销后这个账号不再有可读依据'};
 const SCOPE_DISCLAIMER='以上都只是现存依据；实际读取仍须通过来源检查，本页不把它当作最终可读结论。';
 const LIST_LIMIT=20;
 const TIMEOUT='操作超时，请重新读取资料共享设置。';
 let allowed=false,epoch=0,stale=false;
 let list=null,listSeq=0,listActive=0;
 let selected=null,record=null,detailToken=0,detailActive=0;
 let targets=null,targetsLoaded=false,targetsNext=null,targetsSeq=0,targetsActive=0;
 let preview=null,previewSeq=0,previewActive=0;
 // Grants are read one bounded page at a time. ``grantsFirst`` is the page the detail
 // read returned (page 1); later pages are replaced, never appended, and the cursors
 // are the only thing kept between pages.
 let grantsFirst=null,grantsPage=null,grantPageIndex=0,grantCursors=[''];
 let grantsSeq=0,grantsActive=0;
 // Typed but not yet submitted values survive a re-render; they are dropped when the
 // selected record, the unlock state or a full re-read changes.
 let draft=blankDraft();
 const notes=new Map();
 // An operation owns the page only while its identity is still the current one.
 const listBusy=()=>listActive!==0&&listActive===listSeq;
 const detailBusy=()=>detailActive!==0&&detailActive===detailToken;
 const targetsBusy=()=>targetsActive!==0&&targetsActive===targetsSeq;
 const previewBusy=()=>previewActive!==0&&previewActive===previewSeq;
 const grantsBusy=()=>grantsActive!==0&&grantsActive===grantsSeq;
 const busy=()=>listBusy()||detailBusy()||targetsBusy()||previewBusy()||grantsBusy();
 function blankDraft(){
  return {grantee:'',owner:'',purposes:['reply'],days:'',classification:''};
 }
 function say(place,text){if(typeof text==='string'&&text)notes.set(place,text);else notes.delete(place);}
 function note(place){return notes.get(place)||'';}
 function forget(...keys){for(const key of keys)notes.delete(key);}
 function forgetRevoke(){for(const key of Array.from(notes.keys()))if(key.startsWith('revoke|'))notes.delete(key);}
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
  if(seconds===null)return '永久有效';
  if(typeof seconds!=='number'||!Number.isFinite(seconds))return '未记录或不可读';
  const date=new Date(seconds*1000);
  return Number.isNaN(date.getTime())?'时间不可读':date.toLocaleString('zh-CN',{hour12:false});
 }
 function kindText(kind){return KIND[kind]||textValue(kind);}
 function purposeText(values){
  const rows=Array.isArray(values)?values:[];
  return rows.length?rows.map(value=>PURPOSE[value]||textValue(value)).join('、'):'未记录用途';
 }
 function purposeHintText(values){
  const rows=Array.isArray(values)?values:[];
  const hints=rows.map(value=>PURPOSE_HINT[value]).filter(text=>typeof text==='string');
  return hints.length?hints.join('；'):'用途未记录，不能判断是否可以用于回复';
 }
 function ownersText(values){
  const rows=Array.isArray(values)?values:[];
  return rows.length?rows.join('、'):'没有登记归属账号';
 }
 function feedback(parent,place){
  const node=document.createElement('span');
  node.className='sharing-feedback';node.setAttribute('role','status');
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
  for(const row of rows){
   const item=document.createElement('li');item.textContent=row;box.append(item);
  }
  parent.append(box);return box;
 }
 function button(parent,label,className){
  const node=document.createElement('button');
  if(className)node.className=className;node.textContent=label;parent.append(node);return node;
 }
 function selectBlock(parent,labelText,options,current,onchange){
  const label=document.createElement('label');label.textContent=labelText;
  const box=document.createElement('select');
  for(const option of options){
   const item=document.createElement('option');
   item.value=option.value;item.textContent=option.label;box.append(item);
  }
  const preferred=options.some(option=>option.value===current)?current:(options.length?options[0].value:'');
  box.value=preferred;
  box.onchange=()=>onchange(box.value);
  label.append(box);parent.append(label);
  return box;
 }
 function call(operation,args,timeoutMessage){
  const request_id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{
    pending.delete(request_id);
    const error=Error(timeoutText(timeoutMessage));
    error.code='timeout';
    reject(error);
   },15000);
   pending.set(request_id,{resolve,reject,timer});
   window.chrome.webview.postMessage({action:'management',operation,arguments:args,request_id});
  });
 }
 function timeoutText(value){return typeof value==='string'&&value?value:TIMEOUT;}
 function setAllowed(value){
  if(value===allowed)return;
  allowed=value;reset();
 }
 function reset(){
  epoch++;listSeq++;targetsSeq++;detailToken++;previewSeq++;
  listActive=0;targetsActive=0;detailActive=0;previewActive=0;
  stale=false;
  list=null;selected=null;record=null;
  targets=null;targetsLoaded=false;targetsNext=null;
  preview=null;draft=blankDraft();
  clearGrants();
  notes.clear();render();
 }
 // Dropping what is shown also retires the replies still on their way: a detail or
 // preview answer that was requested earlier may never paint a dropped record back.
 function clearPreview(){
  preview=null;previewSeq++;previewActive=0;forget('preview');
 }
 // Grant pagination belongs to one record's detail snapshot, so every way of dropping
 // or replacing that snapshot retires the pages and any page read still in flight.
 function clearGrants(){
  grantsFirst=null;grantsPage=null;grantPageIndex=0;grantCursors=[''];
  grantsSeq++;grantsActive=0;forget('grants');
 }
 function applyGrantPage(value){
  const rows=value&&Array.isArray(value.grants)?value.grants:[];
  const next=value&&typeof value.grants_next==='string'&&value.grants_next?value.grants_next:null;
  grantsFirst={grants:rows,next};
  grantsPage=null;grantPageIndex=0;grantCursors=[''];
  if(next)grantCursors[1]=next;
  grantsSeq++;grantsActive=0;forget('grants');
 }
 function currentGrantPage(){
  const page=grantPageIndex===0?grantsFirst:grantsPage;
  return {grants:page&&Array.isArray(page.grants)?page.grants:[],
   next:page&&typeof page.next==='string'&&page.next?page.next:null};
 }
 function dropSelection(message){
  selected=null;record=null;detailToken++;detailActive=0;
  clearPreview();draft=blankDraft();
  clearGrants();
  forget('detail','grant','grantee','owner','purpose','days','classify','classify-action');
  forgetRevoke();
  if(typeof message==='string'&&message)say('detail',message);
 }
 // --- listing ---------------------------------------------------------------
 function renderList(){
  const box=el('items');box.replaceChildren();
  if(!allowed)return;
  if(!list){paragraph(box,'还没有读取可管理的资料。点“读取可管理的资料”开始。','note');return;}
  if(!list.records.length){
   paragraph(box,list.offset
    ?'没有更多资料了。'
    :'本机还没有可以在这里调整共享范围的资料。普通记忆、知识和日记会出现在这里；'+
     '共用人物设定与个人事实仍走各自的流程。','note');
   return;
  }
  for(const item of list.records){
   const row=button(box,item.label+' · '+textValue(item.classification_label)+' · 授权 '+
    textValue(item.grants)+' 个'+(item.grants_limited===true?'（可能更多）':''),'quiet');
   row.disabled=!allowed||listBusy()||previewBusy();
   if(selected&&selected.kind===item.kind&&selected.identifier===item.identifier)
    row.setAttribute('aria-current','true');
   row.onclick=()=>{void openRecord(item.kind,item.identifier);};
  }
  paragraph(box,'这里只显示权限、来源数量和已有授权数量，不显示正文；正文仍在“记忆、知识与日记”页查看。','note');
 }
 // --- detail ----------------------------------------------------------------
 async function openRecord(kind,identifier){
  if(!allowed||typeof kind!=='string'||typeof identifier!=='string'||listBusy()||previewBusy())return;
  // A new selection owns a fresh token, so an older reply can never paint it back.
  const current=epoch,token=++detailToken;
  selected={kind,identifier};record=null;detailActive=token;
  clearPreview();draft=blankDraft();clearGrants();
  forget('detail','grant','grantee','owner','purpose','days','classify','classify-action');
  forgetRevoke();
  say('detail','正在读取这条资料的共享设置……');
  render();
  try{
   const value=await call('sharing.record',{kind,identifier},'读取超时，请重新选择这条资料。');
   if(epoch!==current||token!==detailToken)return;
   record=value;stale=false;forget('detail');
   applyGrantPage(value);
   if(!targetsLoaded)await loadTargets('');
  }catch(error){
   if(epoch!==current||token!==detailToken)return;
   if(error.code==='desktop_locked'){setAllowed(false);return;}
   record=null;clearGrants();
   say('detail',(error.message||'这条资料未能读取。')+' 可以重新选择这条资料重试。');
  }finally{
   if(epoch===current&&token===detailToken){detailActive=0;render();}
  }
 }
 async function loadTargets(after){
  if(!allowed||targetsBusy())return;
  const current=epoch,seq=++targetsSeq,page=typeof after==='string'?after:'';
  targetsActive=seq;
  render();
  try{
   const value=await call('sharing.targets',{after:page},'读取已登记账号超时，请重试。');
   if(epoch!==current||seq!==targetsSeq)return;
   const rows=value&&Array.isArray(value.targets)?value.targets:[];
   targets=page&&Array.isArray(targets)?targets.concat(rows):rows;
   targetsNext=value&&typeof value.next==='string'&&value.next?value.next:null;
   targetsLoaded=true;forget('grantee');
  }catch(error){
   if(epoch!==current||seq!==targetsSeq)return;
   if(error.code==='desktop_locked'){setAllowed(false);return;}
   // A failed first read leaves no account list at all; a failed "more" read keeps
   // the accounts already read, so the same button can retry the same page.
   if(!Array.isArray(targets)){targets=null;targetsLoaded=false;}
   say('grantee',(error.message||'已登记账号未能读取。')+' 可以点“重新读取已登记账号”重试。');
  }finally{
   if(epoch===current&&seq===targetsSeq){targetsActive=0;render();}
  }
 }
 function policyLines(policy){
  if(!policy)return ['当前权限记录缺失。'];
  const rows=['分类：'+textValue(policy.classification_label)+'（'+textValue(policy.classification)+'）',
   '归属账号：'+ownersText(policy.owners),
   '会话：'+textValue(policy.conversation)+'；可见范围：'+textValue(policy.audience),
   '有效：'+textValue(policy.valid)+'；到期：'+timeText(policy.expires_at),
   '直接来源：'+textValue(policy.source_count)+' 项'];
  if(policy.pins_complete===false)rows.push('来源版本记录不完整，共享不会让这条资料生效。');
  if(policy.sources_limited===true)rows.push('直接来源超过显示上限，本页只看到其中一部分。');
  return rows;
 }
 function grantStateText(grant){
  if(!grant)return '状态未记录';
  if(grant.revoked===true)return '已撤销';
  if(grant.epoch_current===false)return '属于早先的权限代次，已不生效';
  if(grant.expired===true)return '已失效（已过期）';
  if(grant.active===true)return '有效（只对列出的用途生效）';
  if(grant.active===false)return '已失效';
  return '状态未记录';
 }
 // A grant covers up to 100 records, but the detail view only lists the first 30 and
 // never claims that is the whole scope: the revocation preview is authoritative.
 function grantScopeNote(grant){
  const total=typeof grant.resource_count==='number'?grant.resource_count:null;
  const shown=Array.isArray(grant.resources)
   ?grant.resources.filter(value=>typeof value==='string') : [];
  if(total===null&&!shown.length)
   return ['这条授权覆盖的资料数量未记录；完整范围只在撤销预览里列出。'];
  const rows=[total===null?'这条授权至少覆盖 '+shown.length+' 条资料'
   :'这条授权覆盖 '+total+' 条资料'];
  if(total!==null&&shown.length&&shown.length<total)
   rows.push('这里只显示其中前 '+shown.length+' 条（已截断），完整范围以撤销预览为准');
  if(total!==null&&total>1)rows.push('撤销会同时影响这些资料');
  return [rows.join('；')+'。'];
 }
 function grantBlock(grant){
  const box=document.createElement('article');box.className='sharing-grant';
  const head=document.createElement('h5');
  head.textContent=(textValue(grant.target||grant.grantee))+' · '+grantStateText(grant);
  box.append(head);
  paragraph(box,'用途：'+purposeText(grant.purposes)+'（'+purposeHintText(grant.purposes)+'）；'+
   '到期：'+timeText(grant.expires_at)+'；依据归属账号：'+textValue(grant.owner),'note');
  if(grant.revoked===true)
   paragraph(box,'这条授权已被撤销：任何用途都不能再通过它读取这条资料。现在是否还能读到，取决于其他授权或这条资料的分类。','note');
  else if(grant.epoch_current===false)
   paragraph(box,'这条授权签发于早先的权限代次，已经不生效；撤销只是清理这条记录。','note');
  else if(grant.expired===true)
   paragraph(box,'这条授权已经过期，不再提供读取依据；撤销只是清理这条记录。','note');
  for(const line of grantScopeNote(grant))paragraph(box,line,'note');
  const actions=document.createElement('div');actions.className='actions';
  const revoke=button(actions,'预览撤销这条授权','secondary');
  revoke.disabled=!allowed||busy()||stale||grant.revoked===true;
  revoke.onclick=()=>{void previewRevoke(grant.id);};
  box.append(actions);box.append(feedback(box,'revoke|'+textValue(grant.id)));
  return box;
 }
 function withTarget(rows){
  return (Array.isArray(rows)?rows:[])
   .filter(row=>row&&typeof row.key==='string'&&row.key)
   .map(row=>({value:row.key,label:typeof row.label==='string'&&row.label?row.label:row.key}));
 }
 function grantForm(){
  const box=document.createElement('div');box.className='sharing-form';
  heading(box,'h5','新增一条私聊授权');
  paragraph(box,'这里只新增一条授权：选中的已登记账号可以在选定的用途下使用这条资料。'+
   '“回复”用途允许在私聊回复中使用，“维护”用途只供维护流程使用、不能用于回复；'+
   '任何用途都不会授权会话、不会启动平台、不会改变回复额度。','note');
  const owners=Array.isArray(record.owner_options)
   ?record.owner_options.filter(value=>typeof value==='string') : [];
  if(!owners.length){
   paragraph(box,'这条资料没有登记归属账号，因此无法新增跨账号授权。'+
    '归属账号不会被这个页面修改，请先在原来的资料流程中确认归属。','note');
   box.append(feedback(box,'owner'));
   return box;
  }
  const accounts=withTarget(targets);
  const accountBox=selectBlock(box,'目标账号（只列出已登记账号；这个账号会用这条资料）',
   accounts,draft.grantee,value=>{draft.grantee=value;});
  if(!targetsLoaded)paragraph(box,'还没有读取到已登记账号，所以暂时不能选择目标账号。','note');
  else if(!accounts.length)
   paragraph(box,'本机还没有登记过的账号；没有登记过的账号不能在这里被授权。','note');
  box.append(feedback(box,'grantee'));
  const moreRow=document.createElement('div');moreRow.className='actions';
  const moreLabel=!targetsLoaded?'重新读取已登记账号'
   :targetsNext?'读取更多已登记账号':'已经读完全部已登记账号';
  const more=button(moreRow,moreLabel,'secondary');
  more.disabled=!allowed||busy()||(targetsLoaded&&!targetsNext);
  more.onclick=()=>{void loadTargets(!targetsLoaded||!targetsNext?'':targetsNext);};
  box.append(moreRow);
  const ownerBox=selectBlock(box,'资料归属账号（本次授权依据的归属；本页不会修改它）',
   owners.map(value=>({value,label:value})),draft.owner||(owners.length===1?owners[0]:''),
   value=>{draft.owner=value;});
  if(owners.length>1)paragraph(box,'这条资料有多个归属账号，必须选择本次授权依据的那一个。','note');
  box.append(feedback(box,'owner'));
  const purposeRow=document.createElement('div');purposeRow.className='sharing-purposes';
  for(const value of ['reply','maintenance']){
   const label=document.createElement('label');
   const input=document.createElement('input');
   input.type='checkbox';input.value=value;input.checked=draft.purposes.includes(value);
   input.onchange=()=>{
    const kept=draft.purposes.filter(item=>item!==value);
    draft.purposes=input.checked?kept.concat([value]):kept;
   };
   label.append(input,document.createTextNode(PURPOSE[value]+'（'+PURPOSE_HINT[value]+'）'));
   purposeRow.append(label);
  }
  box.append(purposeRow);box.append(feedback(box,'purpose'));
  const limit=record.days&&typeof record.days.max==='number'&&record.days.max>0?record.days.max:365;
  const initial=record.days&&typeof record.days.default==='number'?record.days.default:30;
  const daysLabel=document.createElement('label');
  daysLabel.textContent='有效期（天，1 至 '+limit+'；到期后这条授权自动失效）';
  const daysInput=document.createElement('input');
  daysInput.type='number';daysInput.min='1';daysInput.max=String(limit);daysInput.step='1';
  daysInput.value=typeof draft.days==='string'&&draft.days?draft.days:String(initial);
  daysInput.oninput=()=>{draft.days=daysInput.value;};
  daysLabel.append(daysInput);box.append(daysLabel);
  box.append(feedback(box,'days'));
  const actions=document.createElement('div');actions.className='actions';
  const run=button(actions,'预览这次授权');
  run.disabled=!allowed||busy()||stale;
  run.onclick=()=>{
   draft.grantee=accountBox.value;draft.owner=ownerBox.value;
   const purposes=draft.purposes.slice();
   forget('grant');
   if(!targetsLoaded){
    say('grantee','请先点“重新读取已登记账号”，再选择目标账号。');say('grant','请先修正上方标出的项目。');
    render();return;
   }
   if(!accounts.length){
    say('grantee','本机还没有登记过的账号，暂时不能新增授权。');say('grant','请先修正上方标出的项目。');
    render();return;
   }
   if(!draft.grantee){say('grantee','请选择一个已登记的目标账号。');say('grant','请先修正上方标出的项目。');
    render();return;}
   if(!draft.owner){say('owner','请选择资料归属账号。');say('grant','请先修正上方标出的项目。');
    render();return;}
   if(!purposes.length){say('purpose','至少选择一项授权用途。');say('grant','请先修正上方标出的项目。');
    render();return;}
   const raw=daysInput.value;
   if(typeof raw!=='string'||!/^(0|[1-9][0-9]{0,8})$/.test(raw)){
    say('days','有效期必须填写 1 至 '+limit+' 的整数，不能留空。');say('grant','请先修正上方标出的项目。');
    render();return;
   }
   const days=Number(raw);
   if(days<1||days>limit){
    say('days','有效期必须在 1 至 '+limit+' 天之间。');say('grant','请先修正上方标出的项目。');
    render();return;
   }
   draft.days=raw;forget('grantee','owner','purpose','days');
   void previewGrant(draft.grantee,draft.owner,purposes,days);
  };
  box.append(actions);box.append(feedback(box,'grant'));
  return box;
 }
 function classifyForm(){
  const box=document.createElement('div');box.className='sharing-form';
  heading(box,'h5','调整资料分类');
  paragraph(box,'分类决定这条资料默认可以被谁使用。调整只改变分类：归属账号、真实来源与版本、'+
   '有效状态和到期时间保持原样；共用基础设定不在此页调整。分类之间没有单一的总序，'+
   '这次调整是让更多还是更少的人读到，只能看预览里的逐场景核对。','note');
  const current=record.policy&&typeof record.policy.classification==='string'
   ?record.policy.classification:'';
  const options=(Array.isArray(record.classifications)?record.classifications:[])
   .filter(item=>item&&typeof item.value==='string')
   .map(item=>({value:item.value,label:typeof item.label==='string'&&item.label?item.label:item.value}));
  if(!options.length){
   paragraph(box,'服务端没有提供可选的分类，因此暂时不能在这里调整。','note');
   box.append(feedback(box,'classify'));
   return box;
  }
  const choice=selectBlock(box,'新的分类（只改变分类本身）',options,
   draft.classification||current,value=>{draft.classification=value;});
  box.append(feedback(box,'classify'));
  const actions=document.createElement('div');actions.className='actions';
  const run=button(actions,'预览这次分类更改','secondary');
  run.disabled=!allowed||busy()||stale;
  run.onclick=()=>{
   draft.classification=choice.value;forget('classify-action');
   if(!draft.classification){
    say('classify','请选择新的分类。');say('classify-action','请先修正上方标出的项目。');
    render();return;
   }
   if(draft.classification===current){
    say('classify','新分类与当前分类相同，没有需要提交的更改。');
    say('classify-action','这次没有需要提交的更改。');render();return;
   }
   forget('classify');
   void previewClassify(draft.classification);
  };
  box.append(actions);box.append(feedback(box,'classify-action'));
  return box;
 }
 function renderDetail(){
  const box=el('detail');box.replaceChildren();
  if(!allowed)return;
  if(!selected){
   paragraph(box,note('detail')||'先在上方选择一条资料，这里会显示它当前的分类、归属、来源和已有授权。','note');
   return;
  }
  if(!record){paragraph(box,note('detail')||'正在读取这条资料的共享设置……','note');return;}
  heading(box,'h3',kindText(record.kind)+' · '+textValue(record.identifier));
  lines(box,policyLines(record.policy));
  paragraph(box,'资料版本：'+textValue(record.record_version)+
   (record.locked?' · 已锁定（锁定只禁止角色自动修改）':''),'note');
  const blockers=Array.isArray(record.blockers)?record.blockers:[];
  const source=record.source||{};
  if(source.ok===true){
   paragraph(box,'来源核对：没有发现会阻断读取的来源问题。','note');
  }else{
   heading(box,'h5','共享现在还不能生效的原因');
   if(!blockers.length)paragraph(box,'来源核对没有通过，但服务端没有给出原因；请先检查这条资料的权限与来源。','note');
   for(const line of blockers)paragraph(box,'· '+textValue(line));
   const issues=Array.isArray(source.issues)?source.issues:[];
   for(const issue of issues.slice(0,20)){
    paragraph(box,'来源：'+textValue(issue&&issue.resource)+' — '+textValue(issue&&issue.detail),'note');
   }
   if(typeof source.total_issues==='number'&&source.total_issues>Math.min(issues.length,20))
    paragraph(box,'阻断项共 '+textValue(source.total_issues)+' 条，只列出前 20 条。','note');
   else if(source.limited===true)paragraph(box,'阻断项超过核对上限，只列出前 20 条。','note');
  }
  if(record.impact)paragraph(box,'依赖影响：'+textValue(record.impact.affected_count)+
   ' 条资料依赖这条资料'+(record.impact.sample_limited===true?'（只列出部分结果）':'')+
   '。确认前会再次核对这些依赖的版本。','note');
  heading(box,'h4','已有私聊授权'+(grantPageIndex>0?'（第 '+(grantPageIndex+1)+' 页）':''));
  const page=currentGrantPage();
  const grants=page.grants;
  const active=typeof record.grants_active==='number'?record.grants_active:null;
  const total=typeof record.grants_total==='number'?record.grants_total:null;
  const revoked=typeof record.grants_revoked==='number'?record.grants_revoked:null;
  const staleEpoch=typeof record.grants_stale_epoch==='number'?record.grants_stale_epoch:null;
  if(active!==null)
   paragraph(box,'服务端统计：这条资料现在有 '+textValue(active)+' 条有效授权'+
    (total!==null?'（共有 '+textValue(total)+' 条授权记录'+
     (revoked!==null?'，其中已撤销 '+textValue(revoked)+' 条':'')+
     (staleEpoch!==null?'，早先权限代次 '+textValue(staleEpoch)+' 条':'')+'）':'')+
    '。一条授权可能同时覆盖多条资料，这里只显示它在这一条资料上的用途与归属。','note');
  paragraph(box,'只列出当前有效的授权；已撤销、已过期或属于早先权限代次的授权不在这一页，'+
   '也不会被这里的撤销改动。','note');
  if(!grants.length)paragraph(box,grantPageIndex===0&&!page.next
   ?'这条资料还没有任何私聊授权。'
   :'这一页没有可以撤销的有效私聊授权。','note');
  for(const grant of grants)box.append(grantBlock(grant));
  if(record.grants_limited===true&&!page.next)
   paragraph(box,'服务端标记这份授权列表被截断，却没有给出下一页位置：其余授权仍然有效，'+
    '这里看不到也不能在此撤销；请重新读取这条资料后重试。','note');
  const nav=document.createElement('div');nav.className='actions';
  const back=button(nav,'上一页授权','secondary');
  back.disabled=!allowed||busy()||grantPageIndex===0;
  back.onclick=()=>{void turnGrantPage(-1);};
  const forward=button(nav,'下一页授权','secondary');
  forward.disabled=!allowed||busy()||!page.next;
  forward.onclick=()=>{void turnGrantPage(1);};
  paragraph(nav,'当前第 '+(grantPageIndex+1)+' 页，本页 '+grants.length+' 条'+
   (page.next?'；还有下一页':'；已经是最后一页'),'note');
  nav.append(feedback(nav,'grants'));
  box.append(nav);
  box.append(grantForm());
  box.append(classifyForm());
  if(typeof record.meaning==='string'&&record.meaning)paragraph(box,record.meaning,'note');
 }
 // --- grant pagination -------------------------------------------------------
 async function turnGrantPage(delta){
  if(!allowed||!selected||!record||grantsBusy()||busy())return;
  const target=grantPageIndex+delta;
  if(target<0)return;
  if(target===0){
   // Page one is the detail snapshot itself: going back needs no request and shows
   // exactly what the last record read returned.
   grantsPage=null;grantPageIndex=0;grantsSeq++;grantsActive=0;forget('grants');
   render();return;
  }
  const after=grantCursors[target];
  if(typeof after!=='string'||!after){
   say('grants','没有到第 '+(target+1)+' 页的位置，请重新读取这条资料。');render();return;
  }
  return readGrantPage(after,target);
 }
 async function readGrantPage(after,target){
  if(!allowed||!selected||grantsBusy())return undefined;
  const current=epoch,seq=++grantsSeq,detailAt=detailToken;
  grantsActive=seq;
  forget('grants');
  say('grants','正在读取第 '+(target+1)+' 页授权……');
  render();
  try{
   const value=await call('sharing.grants',
    {kind:selected.kind,identifier:selected.identifier,after},'读取授权分页超时，请重试。');
   if(epoch!==current||seq!==grantsSeq||detailAt!==detailToken)return undefined;
   const rows=value&&Array.isArray(value.grants)?value.grants:[];
   const next=value&&typeof value.next==='string'&&value.next?value.next:null;
   grantsPage={grants:rows,next,after};
   grantCursors[target]=after;
   if(next)grantCursors[target+1]=next;else delete grantCursors[target+1];
   grantPageIndex=target;
   forget('grants');
  }catch(error){
   if(epoch!==current||seq!==grantsSeq||detailAt!==detailToken)return undefined;
   if(error.code==='desktop_locked'){setAllowed(false);return undefined;}
   // The shown page keeps its own number and contents: a failed read never relabels
   // the old page as the one that failed.
   say('grants',(error.message||'这一页授权没有读到。')+' 当前仍停在第 '+
    (grantPageIndex+1)+' 页，可以再点“下一页授权”重试。');
  }finally{
   if(epoch===current&&seq===grantsSeq){grantsActive=0;render();}
  }
  return undefined;
 }
 // --- preview and commit -----------------------------------------------------
 function effectLabel(check){
  const name=check&&typeof check.shape==='string'?check.shape:'';
  const shape=check&&typeof check.label==='string'&&check.label?check.label
   :Object.prototype.hasOwnProperty.call(SHAPE,name)?SHAPE[name]:textValue(name||null);
  const purpose=check&&typeof check.purpose_label==='string'&&check.purpose_label?check.purpose_label
   :check&&typeof check.purpose==='string'&&check.purpose
    ?(PURPOSE[check.purpose]||textValue(check.purpose)):'';
  // The shape label alone cannot separate "reply" from "maintenance"; both are named.
  return purpose&&shape.indexOf(purpose)<0?shape+' · '+purpose:shape;
 }
 function effectVerdict(check){
  if(!check||typeof check.allowed!=='boolean')return '无法判断';
  return check.allowed?'可以读取':'不能读取';
 }
 function effectLines(effect){
  const rows=Array.isArray(effect)?effect:[];
  if(!rows.length)return ['服务端没有返回逐场景的可读核对，因此本页不判断这个账号现在的可读范围。'];
  return rows.map(check=>effectLabel(check)+'：'+effectVerdict(check));
 }
 function effectSummary(effect){
  const rows=Array.isArray(effect)?effect:[];
  if(!rows.length)return '变更后是否立即生效：无法按账号核对；共享本身不会绕过来源与分类。';
  const verdicts=rows.map(check=>check&&check.allowed);
  if(verdicts.every(value=>value===true))return '变更后：按服务端核对，列出的场景都能读取。';
  if(verdicts.every(value=>value===false))return '变更后：按服务端核对，列出的场景都不能读取；共享不会绕过来源与分类限制。';
  if(verdicts.some(value=>typeof value!=='boolean'))
   return '变更后：部分场景无法判断，其余以逐场景核对为准。';
  return '变更后：只有部分场景能读取，其余仍会被拒绝。';
 }
 function configuredPurposes(value){
  if(value&&Array.isArray(value.configured_purposes))
   return value.configured_purposes.filter(item=>typeof item==='string');
  const effect=Array.isArray(value&&value.effect)?value.effect:[];
  return effect.map(check=>check&&check.purpose).filter(item=>typeof item==='string');
 }
 function replyCapabilityText(value){
  const effect=Array.isArray(value&&value.effect)?value.effect:[];
  const purposes=configuredPurposes(value);
  if(!purposes.length)return '是否可用于回复：服务端没有标注这次操作的用途，因此本页不判断。';
  if(!purposes.includes('reply'))
   return '是否可用于回复：这次操作不包含“回复”用途，因此不能用于回复。';
  const reply=effect.filter(check=>check&&check.purpose==='reply');
  if(!reply.length)
   return '是否可用于回复：这次操作包含“回复”用途，但服务端没有返回对应的核对结果，本页不判断。';
  const privateReply=reply.filter(check=>check&&(check.shape==='private_reply'||check.shape==='private'));
  if(privateReply.some(check=>check.allowed===true))
   return '是否可用于回复：这次操作包含“回复”用途，且归属账号私聊该用途可以读取。';
  return '是否可用于回复：这次操作包含“回复”用途，但按当前状态仍不能读取（来源、分类或授权代次会拒绝）。';
 }
 function grantPreviewLines(value){
  const before=value.before||{},after=value.after||{},grant=after.grant||{};
  return ['当前有效私聊授权：'+textValue(before.grants)+' 条'+
   (before.grants_limited===true?'（服务端只统计了前一部分，数字可能不完整）':''),
   '提交后：'+textValue(after.grants)+' 条有效授权（只是授权数量，不等于一定能读取）',
   '目标账号：'+textValue(grant.target||grant.grantee)+'；依据归属账号：'+textValue(grant.owner),
   '授权用途：'+purposeText(grant.purposes)+'（'+purposeHintText(grant.purposes)+'）',
   '有效期：'+textValue(grant.days)+' 天，到期 '+timeText(grant.expires_at)];
 }
 function classifyPreviewLines(value){
  const before=value.before||{},after=value.after||{};
  const rows=['原分类：'+textValue(before.classification_label)+'（'+textValue(before.classification)+'）',
   '新分类：'+textValue(after.classification_label)+'（'+textValue(after.classification)+'）'];
  if(typeof value.direction==='string'&&value.direction)
   rows.push('服务端标记的方向：'+value.direction+
    '（分类之间没有单一总序：标记为收窄也可能让另一类读者获得访问，实际可读范围以下面的逐场景核对为准）');
  rows.push('保持原样：归属账号 '+ownersText(after.owners)+'；直接来源 '+
   textValue(after.source_count)+' 项；有效 '+textValue(after.valid)+
   '；到期 '+timeText(after.expires_at));
  const basis=value.target_basis||value.owner_checked;
  if(typeof basis==='string'&&basis)rows.push('用于核对的账号：'+basis);
  return rows;
 }
 function revokePreviewLines(value){
  const before=value.before||{},after=value.after||{},grant=before.grant||{};
  const rows=['撤销对象：'+textValue(grant.target||grant.grantee)+'（依据归属账号 '+textValue(grant.owner)+'）',
   '这条授权的用途：'+purposeText(grant.purposes)+'（'+purposeHintText(grant.purposes)+'）',
   '这条授权的状态：'+grantStateText(grant)+'；到期：'+timeText(grant.expires_at),
   '当前有效私聊授权：'+textValue(before.grants)+' 条；撤销后：'+textValue(after.grants)+' 条'];
  if(grant.active===false)
   rows.push('注意：这条授权当前已经失效，撤销它不会减少当前生效的授权数量。');
  if(grant.epoch_current===false)
   rows.push('注意：这条授权属于早先的权限代次，已经不提供读取依据。');
  return rows;
 }
 // --- the whole scope one revocation would touch ------------------------------
 // ``preview.before.grant.resources`` is cut to the first 30 entries, so it is never
 // used as the revocation scope. ``preview.scope.resources`` is the authoritative,
 // complete list (the backend refuses to preview more than its own hard cap), and the
 // page refuses to confirm anything it cannot verify as complete.
 // The backend's own ``state_labels`` wording is deliberately not quoted any more: the
 // page states each surviving basis itself and never claims a final readability.
 function countMarker(value,key){
  const raw=value?value[key]:undefined;
  if(raw===undefined||raw===null)return {present:false,value:null};
  if(typeof raw!=='number'||!Number.isFinite(raw)||raw<0)return {present:true,value:null};
  return {present:true,value:raw};
 }
 function revokeScope(value){
  const scope=value&&value.scope;
  const incomplete=(entries,reason)=>({entries,complete:false,reason});
  if(!scope||typeof scope!=='object')return incomplete([],'这次预览没有返回撤销范围。');
  const rows=Array.isArray(scope.resources)?scope.resources:[];
  if(!rows.length)return incomplete([],'这次预览返回的撤销范围没有任何资料。');
  if(rows.some(row=>!row||typeof row!=='object'||typeof row.resource!=='string'||!row.resource))
   return incomplete(rows,'撤销范围里有条目缺少资料标识。');
  const seen=new Set();
  for(const row of rows){
   if(seen.has(row.resource))
    return incomplete(rows,'撤销范围里有重复的资料（'+row.resource+'）。');
   seen.add(row.resource);
  }
  const outer=countMarker(value,'scope_count'),inner=countMarker(scope,'count');
  if(outer.present&&outer.value===null)
   return incomplete(rows,'这次预览的 scope_count 不是有效的条数。');
  if(inner.present&&inner.value===null)
   return incomplete(rows,'这次预览的 scope.count 不是有效的条数。');
  if(outer.present&&inner.present&&outer.value!==inner.value)
   return incomplete(rows,'服务端两处标记的范围条数不一致（scope_count='+textValue(outer.value)+
    '，count='+textValue(inner.value)+'）。');
  const counted=outer.present?outer.value:inner.present?inner.value:null;
  if(counted!==null&&counted!==rows.length)
   return incomplete(rows,'服务端标记的范围条数（'+textValue(counted)+'）与返回的条目数（'+
    rows.length+'）不一致。');
  return {entries:rows,complete:true,reason:''};
 }
 function scopeEntryText(entry){
  const rows=[textValue(entry.resource)];
  const present=entry.present===true?'资料本体存在'
   :entry.present===false?'资料本体已不存在':null;
  if(present)rows.push(present);
  const policy=entry.policy&&typeof entry.policy==='object'?entry.policy:null;
  if(policy){
   rows.push('分类 '+(typeof policy.classification_label==='string'?policy.classification_label
    :textValue(policy.classification)));
   rows.push('有效 '+textValue(policy.valid));
   rows.push('到期 '+timeText(policy.expires_at));
  }else rows.push('权限记录缺失，无法核对这条资料的现状');
  const state=typeof entry.remaining_basis==='string'?entry.remaining_basis:'';
  rows.push('现存依据：'+(SCOPE_STATE[state]
   ||(state?'未识别的依据类型（'+state+'）':'未记录')));
  if(typeof entry.grantee_grants_active==='number')
   rows.push('该账号另有 '+entry.grantee_grants_active+' 条当前有效授权');
  if(typeof entry.other_grants==='number'&&entry.other_grants>0)
   rows.push('其他授权记录共 '+entry.other_grants+' 条（包括历史记录及其他账号）');
  return rows.join('；');
 }
 function dependentsLines(value){
  const rows=Array.isArray(value.scope_dependents)
   ?value.scope_dependents.filter(item=>typeof item==='string') : [];
  const limited=value.scope_dependents_limited===true;
  const count=value.scope_dependents_count;
  const out=['下游依赖：'+(count===undefined||count===null?'数量未记录':textValue(count))+
   ' 条资料以这条授权覆盖的资料为来源'];
  if(rows.length)out.push('下游资料：'+rows.join('、'));
  if(limited)out.push('下游依赖超过展示上限，只列出前 '+rows.length+' 条；其余没有展示。'+
   '撤销只改这一条授权，不会改动下游资料本身；它们各自能否读取仍由自己的权限与来源决定，本页不推断。');
  return out;
 }
 function previewTitle(value){
  return value.action==='grant'?'确认新增私聊授权'
   :value.action==='classify'?'确认调整分类':'确认撤销授权';
 }
 function renderPreview(){
  const box=el('preview'),wrap=el('preview-area');
  box.replaceChildren();
  const message=note('preview');
  // A preview note (a failure, a timeout, a running check) must stay visible even
  // when there is no preview object yet; hiding it would report nothing at all.
  if(!allowed||(!preview&&!message)){wrap.hidden=true;return;}
  wrap.hidden=false;
  if(!preview){paragraph(box,message,'note');return;}
  const revoke=preview.action==='revoke';
  const scope=revoke?revokeScope(preview):null;
  heading(box,'h4',previewTitle(preview));
  paragraph(box,'这一步还没有写入任何更改：只有点“确认提交”才会提交这次预览的服务端口令。','note');
  const rows=preview.action==='grant'?grantPreviewLines(preview)
   :preview.action==='classify'?classifyPreviewLines(preview):revokePreviewLines(preview);
  lines(box,rows);
  if(revoke){
   heading(box,'h5','撤销范围（整条授权的全部资料）');
   if(scope.complete){
    paragraph(box,'这条授权覆盖 '+scope.entries.length+
     ' 条资料，下面逐条列出；确认前服务端会重新核对整份范围与每条的现存依据，变化后拒绝提交。','note');
    lines(box,scope.entries.map(scopeEntryText),'sharing-scope');
   }else{
    paragraph(box,'没有完整撤销范围：'+scope.reason+
     '因此这里不能确认撤销——不让你在看不到全部受影响资料时提交。可以取消这次预览，或重新预览这条授权。','note');
    if(scope.entries.length)lines(box,scope.entries.map(scopeEntryText),'sharing-scope');
   }
   paragraph(box,SCOPE_DISCLAIMER,'note');
   for(const line of dependentsLines(preview))paragraph(box,line,'note');
   paragraph(box,'撤销按整条授权生效，不会改变分类、来源或其他授权。','note');
  }
  const source=preview.source||{};
  if(source.ok===true)paragraph(box,'来源核对：没有发现会阻断读取的来源问题。','note');
  else{
   paragraph(box,'来源核对没有通过：','note');
   const blockers=Array.isArray(preview.blockers)?preview.blockers:[];
   if(!blockers.length)paragraph(box,'服务端没有给出阻断原因，请先检查这条资料的权限与来源。','note');
   for(const line of blockers)paragraph(box,'· '+textValue(line));
   for(const issue of (Array.isArray(source.issues)?source.issues:[]).slice(0,20)){
    paragraph(box,'来源：'+textValue(issue&&issue.resource)+' — '+textValue(issue&&issue.detail),'note');
   }
  }
  heading(box,'h5','按变更后的状态逐场景核对');
  lines(box,effectLines(preview.effect));
  paragraph(box,effectSummary(preview.effect),'note');
  if(typeof preview.effect_note==='string'&&preview.effect_note)paragraph(box,preview.effect_note,'note');
  if(preview.action==='grant')paragraph(box,replyCapabilityText(preview),'note');
  const impact=preview.impact||{};
  paragraph(box,'依赖影响：'+textValue(impact.affected_count)+' 条资料依赖这条资料'+
   (impact.sample_limited===true?'（只列出部分结果）':'')+'；确认时会重新核对版本与来源，变化后拒绝提交。','note');
  if(preview.action==='classify')
   paragraph(box,'分类之间没有单一的总序，本页不按分类顺序推断扩大或收窄；只按上面的逐场景核对描述可读范围。','note');
  const actions=document.createElement('div');actions.className='actions';
  const confirm=button(actions,'确认提交');
  // A revocation whose scope is not fully shown can never be confirmed from here.
  const refuses=Boolean(revoke&&scope&&!scope.complete);
  confirm.disabled=!allowed||busy()||stale||refuses;
  confirm.onclick=()=>{void commitPreview();};
  const cancel=button(actions,'取消这次预览','secondary');
  cancel.disabled=!allowed||previewBusy()||listBusy();
  cancel.onclick=()=>{void cancelPreview();};
  box.append(actions);box.append(feedback(box,'preview'));
  if(refuses)paragraph(box,'“确认提交”已禁用：这次预览没有完整的撤销范围。取消这次预览后，其他操作仍可继续使用。','note');
 }
 async function runPreview(operation,args,startText,failureText,clear){
  if(!allowed||busy()||stale||!record)return;
  const current=epoch,op=++previewSeq;
  previewActive=op;preview=null;
  for(const key of clear)forget(key);
  forgetRevoke();
  say('preview',startText);
  render();
  try{
   const value=await call(operation,args,'预览超时，请重新预览。');
   if(epoch!==current||op!==previewSeq)return;
   preview=value;forget('preview');
  }catch(error){
   if(epoch!==current||op!==previewSeq)return;
   if(error.code==='desktop_locked'){setAllowed(false);return;}
   say('preview',(error.message||failureText)+' 可以重新核对后再预览。');
  }finally{
   if(epoch===current&&op===previewSeq){previewActive=0;render();}
  }
 }
 function previewGrant(grantee,owner,purposes,days){
  if(!record)return undefined;
  return runPreview('sharing.preview_grant',
   {kind:record.kind,identifier:record.identifier,grantee,owner,purposes,days},
   '正在核对这条授权的影响……','预览没有完成，请重新核对后重试。',
   ['grantee','owner','purpose','days','grant']);
 }
 function previewClassify(classification){
  if(!record)return undefined;
  return runPreview('sharing.preview_classify',
   {kind:record.kind,identifier:record.identifier,classification},
   '正在核对这次分类调整的影响……','预览没有完成，请重新核对后重试。',
   ['classify','classify-action']);
 }
 function previewRevoke(grantId){
  if(!record||typeof grantId!=='string'||!grantId)return undefined;
  return runPreview('sharing.preview_revoke',
   {kind:record.kind,identifier:record.identifier,grant_id:grantId},
   '正在核对这次撤销的影响……','预览没有完成，请重新核对后重试。',[]);
 }
 // "The operation finished" and "what is still readable now" are reported apart: a
 // revoked grant can still leave the account reading through another grant or through
 // a public/personal classification, and that is not a failed revocation.
 function commitText(value){
  const action=value.action;
  const rows=[action==='revoke'?'操作完成：已撤销这条授权。'
   :action==='classify'?'操作完成：已改变这条资料的分类。'
   :'操作完成：已新增这条私聊授权。'];
  const effect=Array.isArray(value.effect)?value.effect:[];
  if(action==='revoke'&&Array.isArray(value.resources)){
   const resources=value.resources.filter(item=>typeof item==='string');
   const shown=resources.slice(0,20);
   rows.push('本次撤销覆盖 '+resources.length+' 条资料：'+shown.join('、')+
    (resources.length>shown.length?'……（共 '+resources.length+' 条，完整清单在确认前的撤销范围里）':'')+'。');
  }
  if(!effect.length){
   rows.push('服务端没有返回逐场景的可读核对，因此本页不判断这个账号现在的可读范围。');
  }else{
   rows.push('服务端逐场景核对：'+effect.map(check=>effectLabel(check)+' '+effectVerdict(check)).join('；')+'。');
   const readable=effect.filter(check=>check&&check.allowed===true);
   if(action==='revoke'){
    rows.push(readable.length
     ?'剩余可读性：这个账号仍能通过其他依据读取其中 '+readable.length+' 项（'+
      readable.map(effectLabel).join('、')+'）；撤销这一条授权本身已经完成，这不代表撤销失败。'
     :'剩余可读性：这些场景现在都不能读取这条资料。');
   }else if(action==='grant'||action==='classify'){
    rows.push(replyCapabilityText(value));
   }
  }
  // The grant notice is written by the backend from "any purpose can read"; it must not
  // be repeated here, because a maintenance-only grant would then read as usable.
  if(action!=='grant'&&typeof value.notice==='string'&&value.notice)
   rows.push('服务端说明：'+value.notice);
  rows.push('列表里的授权数量会在下次读取资料时刷新。');
  return rows.join(' ');
 }
 async function commitPreview(){
  if(!allowed||stale||previewBusy()||listBusy()||!preview)return;
  // Defence in depth: a revocation is never committed unless its whole scope was
  // shown, whichever way the confirm handler was reached.
  if(preview.action==='revoke'){
   const scope=revokeScope(preview);
   if(!scope.complete){
    say('panel','这次预览没有完整的撤销范围（'+scope.reason+'），因此没有提交任何更改；请取消或重新预览。');
    render();return;
   }
  }
  const current=epoch,op=++previewSeq,value=preview;
  previewActive=op;
  forget('preview');
  say('preview','正在提交，服务端会重新核对资料、权限、依赖与来源……');
  render();
  let outcome=null,refresh=false;
  try{
   outcome=await call('sharing.commit',{token:value.token},
    '提交超时：写入结果未知，这次没有自动重试；请重新读取这条资料确认当前状态。');
   if(epoch!==current||op!==previewSeq)return;
   preview=null;forget('preview');
  }catch(error){
   if(epoch!==current||op!==previewSeq)return;
   preview=null;forget('preview');
   if(error.code==='desktop_locked'){setAllowed(false);return;}
   refresh=true;
   if(error.code==='timeout'){
    // The commit may or may not have been written. Never resend the same intent with a
    // new request id: re-read the record and let the administrator decide again.
    stale=true;say('panel',error.message);
   }else if(error.code==='edit_conflict'||error.code==='sharing_preview_expired'
    ||error.code==='request_id_conflict'){
    stale=true;
    say('panel',(error.message||'这次预览已失效。')+' 已标记为需要重新读取，不会自动重新提交。');
   }else say('panel',(error.message||'提交没有完成。')+' 没有自动重试；请重新读取资料后重试。');
  }finally{
   if(epoch===current&&op===previewSeq){previewActive=0;render();}
  }
  if(epoch!==current||op!==previewSeq)return;
  if(outcome){
   say('panel',commitText(outcome));
   refresh=true;
  }
  if(refresh)await reloadRecord();
 }
 async function cancelPreview(){
  if(!allowed||!preview||previewBusy()||listBusy())return;
  const value=preview;
  // Clearing first makes a second click a no-op, so a cancel can never be sent twice.
  preview=null;previewSeq++;previewActive=0;
  forget('preview');
  say('panel','已取消这次预览，没有写入任何更改。');
  render();
  try{
   await call('sharing.cancel',{token:value.token},
    '取消请求超时；服务端预览会自行过期，本页不会再提交这次预览。');
  }catch(error){
   // The token expires on the server anyway; the page already stopped.
   if(error.code==='desktop_locked')setAllowed(false);
  }
 }
 async function reloadRecord(){
  if(!allowed||!selected)return;
  const current=epoch,token=++detailToken;
  detailActive=token;forget('detail');
  render();
  try{
   const value=await call('sharing.record',
    {kind:selected.kind,identifier:selected.identifier},'重新读取超时，请重新选择这条资料。');
   if(epoch!==current||token!==detailToken)return;
   record=value;stale=false;draft=blankDraft();forgetRevoke();
   // A confirmed change re-reads the detail, so the grant pages start over at page one.
   applyGrantPage(value);
  }catch(error){
   if(epoch!==current||token!==detailToken)return;
   record=null;
   if(error.code==='desktop_locked'){setAllowed(false);return;}
   say('detail',(error.message||'这条资料未能重新读取。')+' 可以重新选择这条资料重试。');
  }finally{
   if(epoch===current&&token===detailToken){detailActive=0;render();}
  }
 }
 // --- listing reads and wiring -----------------------------------------------
 async function loadPage(after){
  if(!allowed||listBusy())return;
  const current=epoch,seq=++listSeq,page=typeof after==='string'?after:'';
  listActive=seq;
  say('panel',page?'正在读取下一页资料……':'正在读取资料……');
  render();
  try{
   const value=await call('sharing.records',{after:page,limit:LIST_LIMIT},
    '读取资料超时，请重新读取资料共享设置。');
   // Nothing is assigned before this check: a reply from before a lock, a backend stop
   // or a newer read must not revive or supersede the page.
   if(epoch!==current||seq!==listSeq)return;
   const rows=value&&Array.isArray(value.records)?value.records:[];
   const kept=page&&list?list.records:[];
   list={records:kept.concat(rows),next:typeof value.next==='string'&&value.next?value.next:null,
    offset:typeof value.offset==='string'?value.offset:''};
   say('panel','已读取 '+list.records.length+' 条可管理的资料。');
   if(selected&&!list.records.some(item=>item&&item.kind===selected.kind
    &&item.identifier===selected.identifier))
    dropSelection('刚才选中的资料已不在这次读取的结果里，已取消选择；需要时请重新选择。');
  }catch(error){
   if(epoch!==current||seq!==listSeq)return;
   if(error.code==='desktop_locked'){setAllowed(false);return;}
   // A failed read leaves nothing editable behind: no old list, detail, draft or preview.
   list=null;dropSelection('');
   say('panel',(error.message||'资料未能读取。')+' 可以点“读取可管理的资料”重试。');
  }finally{
   if(epoch===current&&seq===listSeq){listActive=0;render();}
  }
 }
 function render(){
  el('load').disabled=!allowed||listBusy();
  el('status').textContent=note('panel')||'';
  renderList();
  el('next').disabled=!allowed||listBusy()||!list||!list.next;
  renderDetail();
  renderPreview();
 }
 el('load').onclick=()=>{void loadPage('');};
 el('next').onclick=()=>{
  const next=list&&list.next;
  if(typeof next==='string'&&next)void loadPage(next);
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
 window.QibanAutoRead?.register('sharing-load',{dirty:()=>!!selected});
})();
