(()=>{
 'use strict';
 const el=id=>document.getElementById(id),pending=new Map();
 let allowed=false,busy=false,epoch=0,items=[],selected=null,dirty=false,preview=null,after='',next=null,back=[];
 let advanced=null,advancedDiff='';
 let timeline=null,factChosen=null,factChecks=new Map(),factControls=[],factAt=null;
 let retention=null;
 let kind='memories';
 const kindNames={memories:'记忆',knowledge:'知识',diaries:'日记'};
 const labels={user_preference:'偏好',experience:'经历',reflection:'思考',explicit:'明确告知',observed:'观察所得',inferred:'推断',user_instruction:'用户说明',conversation:'对话',video:'视频',interaction:'互动',web:'网页',public:'公开',private:'私密',shared:'共享',secret:'机密',unclassified:'待审核'};
 const notice=text=>{el('memory-notice').textContent=text;};
 const advancedNotice=text=>{el('memory-advanced-notice').textContent=text;};
 const scopes={public:'公开',conversation:'限指定会话',personal:'个人资料',restricted:'受限资料',secret:'机密',unclassified:'待审核'};
 const audiences={private:'私聊',group:'群聊',public:'公开场合'};
 const attributes={residence:'居住地',preferred_name:'偏好称呼',drink_preference:'饮品偏好',food_preference:'饮食偏好'};
 const factDraft=()=>!!(el('fact-value').value.trim()||el('fact-reason').value.trim());
 function call(operation,args={},native=false){
  const id=crypto.randomUUID().replaceAll('-',''),token=epoch;
  return new Promise((resolve,reject)=>{
   // A native file picker stays open as long as the person needs; ordinary management keeps 15s.
   const timer=setTimeout(()=>{pending.delete(id);reject(new Error(native?'文件选择或读写未及时返回，请重新读取确认结果。':'操作未及时返回，请重新读取确认结果。'));},native?300000:15000);
   pending.set(id,{resolve,reject,timer,epoch:token});
   window.chrome.webview.postMessage(native?{action:operation,request_id:id,...args}:
    {action:'management',operation:operation.startsWith('facts.')?operation:'records.'+operation,arguments:args,request_id:id});
  });
 }
 function controls(){
  const advancedOpen=!!advanced,fact=!!selected?.data.fact;
  el('memory-kind').disabled=!allowed||busy||dirty||factDraft()||advancedOpen;
  el('memory-kind').value=kind;
  el('memory-save').textContent='保存'+kindNames[kind];
  el('memory-refresh').textContent='重新读取'+kindNames[kind];
  el('memory-prev').textContent='上一页'+kindNames[kind];
  el('memory-next').textContent='下一页'+kindNames[kind];
  el('fact-retention-read').disabled=!allowed||busy;
  el('fact-retention-days').disabled=!allowed||busy||!retention;
  el('fact-retention-save').disabled=!allowed||busy||!retention||Number(el('fact-retention-days').value)===retention.days;
  for(const id of ['memory-refresh','memory-prev','memory-next'])el(id).disabled=!allowed||busy||dirty||factDraft()||advancedOpen;
  el('memory-prev').disabled ||= back.length===0;
  el('memory-next').disabled ||= !next;
  for(const id of ['memory-content','memory-tags'])el(id).disabled=!allowed||busy||!selected||fact||advancedOpen;
  el('memory-save').disabled=!allowed||busy||!selected||!dirty||fact||advancedOpen;
  el('memory-discard').disabled=!allowed||busy||!dirty||advancedOpen;
  el('memory-explain').disabled=!allowed||busy||!selected||dirty||advancedOpen;
  el('memory-delete-preview').disabled=!allowed||busy||!selected||dirty||factDraft()||!!selected?.retirement||advancedOpen;
  el('memory-delete-preview').textContent=selected?.data.fact?'查看停用影响':'查看删除影响';
  el('memory-delete-confirm').textContent=selected?.data.fact?'确认停用此属性的既有事实':'确认删除当前'+kindNames[kind];
  el('memory-delete-confirm').disabled=!allowed||busy||!preview||dirty||advancedOpen;
  el('memory-delete-confirm').hidden=!preview;
  el('memory-delete-cancel').hidden=!preview;
  el('memory-delete-cancel').disabled=!allowed||busy||advancedOpen;
  for(const button of el('memory-items').children)button.disabled=!allowed||busy||dirty||factDraft()||advancedOpen;
  el('memory-draft').textContent=dirty?'有尚未保存的修改。保存或放弃修改后可切换条目。':'';
  const blocked=!allowed||busy||dirty||!fact;
  el('fact-area').hidden=!fact;
  el('fact-at').disabled=blocked||factDraft()||!!selected?.retirement;
  el('fact-refresh').disabled=blocked||factDraft();
  el('fact-next').disabled=blocked||!timeline?.next;
  el('fact-reason').disabled=blocked||!factChosen;
  el('fact-value').disabled=blocked||!factChosen;
  el('fact-clear').disabled=blocked||!timeline;
  const canSubmit=!blocked&&!!factChosen&&factChecks.size>0&&factChecks.size<=20&&!!el('fact-reason').value.trim()&&!factAt;
  el('fact-confirm').disabled=!canSubmit||!!el('fact-value').value.trim();
  el('fact-correct').disabled=!canSubmit||!el('fact-value').value.trim()||el('fact-value').value.trim()===factChosen?.value;
  for(const control of factControls)control.node.disabled=blocked||!!factAt||!control.eligible();
  const advancedBlocked=!allowed||busy||dirty||!selected||fact||advancedOpen;
  el('memory-advanced-export').disabled=advancedBlocked;
  el('memory-advanced-import').disabled=advancedBlocked;
  el('memory-advanced-confirm').disabled=!allowed||busy||dirty||!advanced||!advanced.changed;
  el('memory-advanced-cancel').disabled=!allowed||busy||!advanced;
 }
 function clearFacts(){
  timeline=null;factChosen=null;factChecks.clear();factControls=[];factAt=null;
  el('fact-items').replaceChildren();el('fact-summary').textContent='';el('fact-choice').textContent='';
  el('fact-reason').value='';el('fact-value').value='';el('fact-at').value='';el('fact-notice').textContent='';
 }
 function showFacts(){
  const panel=el('fact-items');panel.replaceChildren();factControls=[];
  const status={active:'当前',disputed:'存在冲突',historical:'历史',future:'尚未生效',disabled:'已明确停用'};
  el('fact-summary').textContent=`核对时点：${timeline.at}；共 ${timeline.total} 条。当前 ${timeline.counts.active}，冲突 ${timeline.counts.disputed}，历史 ${timeline.counts.historical}，未来 ${timeline.counts.future}，已停用 ${timeline.counts.disabled||0}。`;
  for(const row of timeline.items){
   const card=document.createElement('div'),text=document.createElement('p');
   text.textContent=`${status[row.status]||row.status} · ${attributes[row.attribute]||row.attribute}：${row.value}\n生效时间：${row.valid_from}\n${row.correction?'原始来源原话（更正前）':'原话'}：${row.quote}\n来源：${row.source.reference}\n${row.correction?'管理员更正：'+row.correction.original_value+' → '+row.value+'；原因：'+row.correction.reason+'\n':''}${row.confirmation?'管理员确认原因：'+row.confirmation.reason:''}`;
   const choose=document.createElement('button');choose.className='secondary';choose.textContent='采用这条事实';
   const current=()=>['active','disputed'].includes(row.status);
   choose.onclick=()=>{if(!allowed||busy||dirty||factAt||factDraft()||!current())return;factChosen=row;factChecks=new Map([[row.id,row]]);showFacts();controls();};
   const label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';check.checked=factChecks.has(row.id);
   check.onchange=()=>{
    if(!allowed||busy||dirty||factAt||!current()||!factChosen||row.attribute!==factChosen.attribute)return;
    if(row.id===factChosen.id){check.checked=true;return;}
    if(check.checked){if(factChecks.size>=20){check.checked=false;el('fact-notice').textContent='一次最多确认 20 条记录。';return;}factChecks.set(row.id,row);}
    else factChecks.delete(row.id);
    showChoice();controls();
   };
   label.append(check,' 将此记录列为被替代项');card.append(text,choose,label);panel.append(card);
   factControls.push({node:choose,eligible:()=>current()&&!factDraft()},{node:check,eligible:()=>current()&&!!factChosen&&row.attribute===factChosen.attribute&&row.id!==factChosen.id});
  }
  showChoice();
 }
 function showChoice(){el('fact-choice').textContent=factChosen?`将采用：${attributes[factChosen.attribute]||factChosen.attribute}：${factChosen.value}；已选择 ${factChecks.size} 条被替代记录（含其他页选择）。`:'';}
 async function loadFacts(cursor,at){
  const value=await call('facts.history',{identifier:selected.data.id,after:cursor,at});
  return ()=>{
   if(cursor&&timeline?.revision!==value.revision){clearFacts();el('fact-notice').textContent='事实或权限已变化，请重新读取后选择。';return;}
   timeline=value;factAt=at;showFacts();el('fact-notice').textContent=at?'正在查看历史时点，不能据此确认当前事实。':'时间线已读取。';
  };
 }
 el('fact-refresh').onclick=()=>{
  if(!selected?.data.fact||dirty||factDraft())return;
  const at=el('fact-at').value.trim()||null;clearFacts();el('fact-at').value=at||'';
  return run(()=>loadFacts('',at));
 };
 el('fact-next').onclick=()=>{if(timeline?.next&&!dirty)return run(()=>loadFacts(timeline.next,factAt));};
 el('fact-at').oninput=()=>{timeline=null;factChosen=null;factChecks.clear();factControls=[];el('fact-items').replaceChildren();el('fact-choice').textContent='';el('fact-summary').textContent='';el('fact-reason').value='';controls();};
 el('fact-reason').oninput=controls;
 el('fact-value').oninput=controls;
 el('fact-clear').onclick=()=>{if(!busy){factChosen=null;factChecks.clear();el('fact-reason').value='';el('fact-value').value='';if(timeline)showFacts();el('fact-notice').textContent='已取消选择，事实未改变。';controls();}};
 el('fact-confirm').onclick=()=>{
  if(!allowed||busy||dirty||!factChosen||factAt||!factChecks.size||factChecks.size>20||!el('fact-reason').value.trim()||el('fact-value').value.trim())return;
  const args={identifier:factChosen.id,expected:timeline.revision,reason:el('fact-reason').value.trim(),supersedes:[...factChecks.keys()]};
  return run(async()=>{
   await call('facts.confirm',args);
   return ()=>{clearFacts();el('fact-notice').textContent='确认已追加，原记录和历史已保留。请重新读取时间线。';};
  });
 };
 el('fact-correct').onclick=()=>{
  if(el('fact-correct').disabled)return;
  const args={identifier:factChosen.id,expected:timeline.revision,reason:el('fact-reason').value.trim(),supersedes:[...factChecks.keys()],value:el('fact-value').value.trim()};
  return run(async()=>{
   await call('facts.correct',args);
   return ()=>{clearFacts();el('fact-notice').textContent='管理员更正已保存，旧值、原话和来源已保留。请重新读取时间线。';};
  },message=>{el('fact-notice').textContent=message;});
 };
 el('fact-retention-days').oninput=controls;
 el('fact-retention-read').onclick=()=>run(async()=>{
  const value=await call('facts.retention');
  return ()=>{
   retention=value;el('fact-retention-days').value=String(value.days);
   const last=value.last_cleanup;
   el('fact-retention-notice').textContent='设置已读取。未停用的历史不按时间清理。'+(value.maintenance_error?'后台清理遇到异常，请查看后台状态。':last?`最近一批检查 ${last.examined} 条，清理 ${last.removed} 条，因引用或有效状态保留 ${last.referenced_or_active} 条。`:'尚未执行后台检查。');
   if(!value.maintenance_error&&last?.snapshots)el('fact-retention-notice').textContent+=last.snapshots.status==='indexing'?'正在分批核对旧记录的引用，完成前暂不清理。':`本批回收 ${last.snapshots.removed} 条失效且未使用的自动来源记录。`;
   if(!value.maintenance_error&&last?.snapshots?.unverified)el('fact-retention-notice').textContent+=`${last.snapshots.unverified} 条来源关系尚未核实，已保留待检查。`;
  };
 },message=>{el('fact-retention-notice').textContent=message;});
 el('fact-retention-save').onclick=()=>{
  if(!retention)return;
  const days=Number(el('fact-retention-days').value);
  if(!Number.isInteger(days)||days<1||days>3650){el('fact-retention-notice').textContent='请填写 1 至 3650 天。';return;}
  return run(async()=>{
   const value=await call('facts.retention_save',{days,expected:retention.revision});
   return ()=>{retention=value;el('fact-retention-notice').textContent='清理设置已保存。仅明确停用且没有引用的过期正文会清理。';};
  },message=>{el('fact-retention-notice').textContent=message;});
 };
 function clearPreview(){preview=null;el('memory-delete-effect').textContent='';}
 function clearExplanation(){el('memory-explanation').replaceChildren();el('memory-explanation').hidden=true;}
 function explain(value){
  const panel=el('memory-explanation');clearExplanation();panel.hidden=false;
  const add=text=>{const row=document.createElement('p');row.textContent=text;panel.append(row);};
  const reasons={revoked:'权限已失效',unclassified:'尚未审核共享范围',secret:'机密资料',expired:'授权已过期',incomplete_source_versions:'来源版本记录不完整',source_missing_or_changed:'来源缺失或已发生变化'};
  add(`来源与权限检查：${value.total_nodes}项资料，发现${value.total_issues}项需关注情况。${value.limited?'资料明细仅展示前200项。':''}`);
  add(value.meaning);
  if(!value.total_issues)add('当前记录未发现上述异常；这不代表任意账号或会话都有权读取。');
  for(const issue of value.issues)add(`${issue.resource}：${reasons[issue.reason]||issue.reason}${issue.source?'；相关来源：'+issue.source:''}`);
  if(value.total_issues>value.issues.length)add(`仅展示前${value.issues.length}项需关注情况。`);
  for(const node of value.nodes){
   add(`资料：${node.resource}\n共享范围：${scopes[node.classification]||node.classification}；权限记录：${node.valid?'有效':'已失效'}\n关联账号：${node.owners.join('、')||'未指定'}\n会话：${node.conversation||'未指定'}；受众：${audiences[node.audience]||node.audience||'未指定'}\n直接来源：${node.source_count}项${node.source_count>node.sources.length?'（仅展示部分）':''}\n${node.sources.join('\n')||'没有记录直接来源'}`);
  }
  add(`修改或删除可能影响${value.impact.affected_count}项资料（包含当前资料）；此次检查没有修改或删除资料。`);
 }
 function clearAdvanced(){advanced=null;advancedDiff='';renderAdvanced();}
 function renderAdvanced(){
  const panel=el('memory-advanced-panel'),fields=el('memory-advanced-fields'),diff=el('memory-advanced-diff');
  panel.hidden=!advanced;
  if(!advanced){
   el('memory-advanced-summary').textContent='';fields.replaceChildren();
   el('memory-advanced-references').textContent='';el('memory-advanced-impact').textContent='';
   el('memory-advanced-changed').textContent='';el('memory-advanced-diff-text').textContent='';
   diff.hidden=true;return;
  }
  const label=kindNames[advanced.kind]||advanced.kind;
  el('memory-advanced-summary').textContent=`即将导入${label}“${advanced.identifier}”：${advanced.changed?'检测到差异，确认后新增版本并保留旧版本':'没有需要保存的差异'}。预览没有写入任何资料。`;
  fields.replaceChildren();
  const changes=advanced.changes||[];
  if(!changes.length){const row=document.createElement('p');row.textContent='字段内容没有变化。';fields.append(row);}
  for(const change of changes){
   const before=change.before||{},after=change.after||{},row=document.createElement('p');
   row.textContent=`${change.field}\n修改前：${before.text||''}${before.truncated?'（仅显示前 240 字）':''}\n修改后：${after.text||''}${after.truncated?'（仅显示前 240 字）':''}`;
   fields.append(row);
  }
  const refs=advanced.references||{},names=values=>values&&values.length?values.join('、'):'无';
  el('memory-advanced-references').textContent=[
   `真实引用：新增 ${(refs.added||[]).length} 项，移除 ${(refs.removed||[]).length} 项，更新 ${(refs.updated||[]).length} 项。`,
   '新增来源：'+names(refs.added),'移除来源：'+names(refs.removed),'更新来源：'+names(refs.updated)].join('\n');
  const impact=advanced.impact||{},sample=impact.sample||[],affected=Number.isInteger(impact.affected_count)?impact.affected_count:0;
  const impactLines=[`修改或保存可能影响 ${affected} 项资料（包含当前资料）。`];
  if(sample.length)impactLines.push(sample.join('\n'));
  impactLines.push(impact.sample_limited?`仅列出前 ${sample.length} 项，其余未展示。`:(sample.length?'以上为全部受影响资料。':'没有其他关联资料。'));
  el('memory-advanced-impact').textContent=impactLines.join('\n');
  el('memory-advanced-changed').textContent=advanced.notice||'';
  el('memory-advanced-diff-text').textContent=advancedDiff;
  diff.hidden=!advancedDiff;
 }
 function showDetail(){
  clearFacts();
  clearExplanation();
  clearAdvanced();
  const data=selected?.data;
  el('memory-editor').hidden=!data;
  el('memory-content').value=data?.content||'';
  el('memory-tags').value=(data?.tags||[]).join('，');
  el('memory-meta').textContent=data?`${labels[data.kind]||kindNames[kind]}${data.confidence?' · '+(labels[data.confidence]||'待核实'):''} · ${scopes[selected.policy?.classification]||'待审核'}${data.locked?' · 已锁定自动修改':''}${selected.retirement?' · 已停用，仅供管理员查看历史':''}`:'';
  el('memory-source').textContent=data?`来源：${labels[data.source?.type]||'其他'} · ${data.source?.recorded_at||'时间未记录'}\n${data.source?.reference||''}`:'';
  const related=[];
  if(data?.coverage){
   const c=data.coverage,names={metadata_only:'仅标题简介',subtitles:'字幕',transcription:'语音转写',frames:'视频画面',web:'网页'};
   related.push(`阅读依据：${names[c.kind]||c.kind}；${c.complete?'已完整读取':'仅部分内容'}${c.bvid?'；视频：'+c.bvid:''}`);
   if(c.kind!=='web')related.push(`覆盖范围：${c.start_seconds}–${c.end_seconds} 秒；总长 ${c.duration_seconds} 秒。`);
  }
  if(data?.disagrees_with?.length)related.push('记录的不同观点关联：'+data.disagrees_with.join('、'));
  if(data?.memory_refs?.length)related.push('关联记忆：'+data.memory_refs.map(ref=>ref.id).join('、')+'。保存正文不会替换这些关联版本。');
  if(data?.source_quotes?.length)related.push('来源原话：\n'+data.source_quotes.map(q=>typeof q==='string'?q:q.quote||'').join('\n'));
  el('memory-related').textContent=related.join('\n');
  dirty=false;clearPreview();controls();
 }
 function renderList(){
  const list=el('memory-items');list.replaceChildren();
  for(const item of items){
   const button=document.createElement('button');button.className='secondary memory-item';
   button.textContent=(item.data.content||'空白'+kindNames[kind]).slice(0,90);
   button.setAttribute('aria-pressed',String(selected?.data.id===item.data.id));
   button.onclick=()=>{
    if(!allowed||busy||dirty||advanced)return;
    return run(async()=>{
     const value=await call('read',{kind,identifier:item.data.id});
     return ()=>{selected=value;showDetail();renderList();notice(kindNames[kind]+'已读取。');};
    });
   };list.append(button);
  }
  el('memory-empty').textContent=items.length?'':`当前没有${kindNames[kind]}。角色产生的${kindNames[kind]}会出现在这里。`;controls();
 }
 async function run(work,onError=notice){
  if(!allowed||busy)return;busy=true;const token=epoch;controls();
  try{const commit=await work(token);if(allowed&&epoch===token)commit?.();}
  catch(error){if(allowed&&epoch===token)onError(error.message);}
  finally{if(epoch===token){busy=false;controls();}}
 }
 async function load(cursor,previous){
  const token=epoch,result=await call('list',{kind,after:cursor,limit:5}),loaded=[];
  for(const row of result.records){
   if(!allowed||epoch!==token)return;
   loaded.push(await call('read',{kind,identifier:row.id}));
  }
  return ()=>{items=loaded;after=cursor;next=result.next;back=previous;selected=null;showDetail();renderList();notice(kindNames[kind]+'列表已更新。');};
 }
 el('memory-kind').onchange=()=>{
  const value=el('memory-kind').value;
  if(!allowed||busy||dirty||factDraft()||advanced||!Object.hasOwn(kindNames,value)){el('memory-kind').value=kind;return;}
  kind=value;items=[];selected=null;after='';next=null;back=[];showDetail();renderList();
  return run(()=>load('',[]));
 };
 el('memory-refresh').onclick=()=>{if(!dirty&&!advanced)return run(()=>load('',[]));};
 el('memory-next').onclick=()=>{if(!dirty&&!advanced&&next)return run(()=>load(next,[...back,after]));};
 el('memory-prev').onclick=()=>{if(!dirty&&!advanced&&back.length)return run(()=>load(back.at(-1),back.slice(0,-1)));};
 for(const id of ['memory-content','memory-tags'])el(id).oninput=()=>{if(!allowed||busy||advanced||selected?.data.fact)return;dirty=true;clearPreview();clearExplanation();clearFacts();notice('');controls();};
 el('memory-explain').onclick=()=>{
  if(!selected||dirty||busy||advanced||!allowed)return;
  clearExplanation();return run(async()=>{
   const value=await call('explain',{kind,identifier:selected.data.id});
   return ()=>{explain(value);notice('来源与权限依据已读取。此结果是本次检查的快照。');};
  });
 };
 el('memory-discard').onclick=()=>{if(!busy&&!advanced){showDetail();notice('已放弃未保存的修改。');}};
 el('memory-save').onclick=()=>{
  if(!selected||!dirty||advanced||selected.data.fact)return;
  const tags=el('memory-tags').value.split(/[,，\n]/).map(v=>v.trim()).filter(Boolean),content=el('memory-content').value;
  if(content.length>16000||tags.length>100||tags.some(v=>v.length>16000)){notice('正文最多16000字，标签最多100个。');return;}
  const value={...selected.data,content,tags},old=selected;
  return run(async()=>{
   const result=await call('correct',{kind,identifier:value.id,value,expected:old.version});
   return ()=>{selected={...old,data:value,version:result.version};items=items.map(item=>item.data.id===value.id?selected:item);showDetail();renderList();notice(kindNames[kind]+'已保存。相关派生资料将在使用前重新检查。');};
  });
 };
 el('memory-delete-preview').onclick=()=>{
  if(!selected||dirty||advanced)return;
  clearPreview();return run(async()=>{
   const result=await call('delete_preview',{kind,identifier:selected.data.id});
   return ()=>{preview=result;el('memory-delete-effect').textContent=`影响范围：${result.affected_count}项资料。${result.effect}`;notice(selected.data.fact?'请核对停用范围，确认后旧值不会恢复。':'请核对删除影响，再确认删除。');};
  });
 };
 el('memory-delete-cancel').onclick=()=>{if(!busy&&!advanced){clearPreview();controls();notice('已取消删除，'+kindNames[kind]+'未改变。');}};
 el('memory-delete-confirm').onclick=()=>{
  if(!preview||dirty||advanced)return;
  const token=preview.token,id=selected.data.id;clearPreview();
  return run(async()=>{
   const result=await call('delete',{token});
   const value=result.retired?await call('read',{kind,identifier:id}):null;
   return ()=>{
    if(result.retired){selected=value;items=items.map(item=>item.data.id===id?value:item);}
    else{items=items.filter(item=>item.data.id!==id);selected=null;}
    showDetail();renderList();notice(result.retired?'事实已停用，旧值不会恢复。历史可继续查看；未明确停用的其他历史不受影响。':'当前'+kindNames[kind]+'已删除；加密历史和既有备份保留。');
   };
  });
 };
 el('memory-advanced-export').onclick=()=>{
  if(!allowed||busy||dirty||advanced||!selected||selected.data.fact)return;
  const target={kind,identifier:selected.data.id,expected_version:selected.version};
  advancedNotice('正在导出当前资料……');
  return run(async()=>{
   const result=await call('record-export',target,true);
   return ()=>{advancedNotice(`已导出到所选位置：${result.filename}（${result.bytes} 字节，含真实来源清单）。这是明文文件，请妥善保管；导出没有修改资料。`);};
  },message=>advancedNotice(message));
 };
 el('memory-advanced-import').onclick=()=>{
  if(!allowed||busy||dirty||advanced||!selected||selected.data.fact)return;
  const target={kind,identifier:selected.data.id,expected_version:selected.version};
  advancedNotice('正在选择文件并读取完整差异，确认前不会写入……');
  return run(async token=>{
   const result=await call('record-import',target,true);
   if(!result||typeof result.token!=='string')throw new Error('导入预览未能读取，请重新选择文件。');
   let offset=0,next=0,text='',pages=0;
   // The confirmation stays unavailable until every difference page has arrived.
   while(next!==null){
    if(!allowed||epoch!==token)return;
    const page=await call('advanced_changes',{token:result.token,offset});
    text+=page.text||'';next=page.next??null;offset=next??0;
    if(++pages>200)throw new Error('完整差异未能本次读取完，请重新预览后再确认。');
   }
   if(!allowed||epoch!==token)return;
   return ()=>{
    // An advanced preview and a delete preview must never stay actionable together.
    clearPreview();
    advanced={...result,kind:target.kind,identifier:target.identifier};advancedDiff=text;
    renderAdvanced();controls();
    advancedNotice(result.changed?'完整差异已读取。确认前不会写入；确认后内容、真实引用和审计一并提交，旧版本保留。':'文件内容与当前资料一致，没有需要保存的差异。');
   };
  },message=>advancedNotice(message));
 };
 el('memory-advanced-cancel').onclick=()=>{
  const current=advanced;
  if(!allowed||busy||!current)return;
  return run(async()=>{
   let message='已取消导入，'+kindNames[current.kind]+'未改变。';
   try{await call('advanced_cancel',{token:current.token});}catch(error){message=error.message;}
   return ()=>{clearAdvanced();controls();advancedNotice(message);};
  },message=>advancedNotice(message));
 };
 el('memory-advanced-confirm').onclick=()=>{
  const current=advanced;
  if(!allowed||busy||dirty||!current||!current.changed)return;
  return run(async()=>{
   const result=await call('advanced_commit',{token:current.token});
   const value=result.changed?await call('read',{kind:current.kind,identifier:current.identifier}):null;
   return ()=>{
    clearAdvanced();advancedNotice('');
    if(value){selected=value;items=items.map(item=>item.data.id===current.identifier?value:item);}
    showDetail();renderList();
    notice(result.changed?'高级导入已提交：正文、真实引用与审计一并保存，旧版本和来源历史保留。':'没有需要保存的差异，未新增版本。');
   };
  },message=>{clearAdvanced();controls();advancedNotice(message);});
 };
 window.addEventListener('qiban-management-state',({detail})=>{
  const value=detail.ready&&!detail.locked,changed=value!==allowed;allowed=value;el('memory-panel').hidden=el('management-panel').hidden;
  if(!changed)return;
  if(!allowed){epoch++;busy=false;items=[];selected=null;after='';next=null;back=[];retention=null;el('fact-retention-days').value='';el('fact-retention-notice').textContent='';showDetail();renderList();notice('');advancedNotice('');}
  else void run(()=>load('',[]));
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management'){
   const job=pending.get(data.request_id);if(!job)return;clearTimeout(job.timer);pending.delete(data.request_id);
   // A reply that belongs to an earlier authorization epoch (lock/unlock) is ignored.
   if(job.epoch!==epoch){job.reject(new Error('管理状态已变化，本次结果已忽略。'));return;}
   if(data.response.ok)job.resolve(data.response.result);else job.reject(new Error(data.response.message||'记忆操作未完成。'));
  }
  if(data.type==='management_error'){for(const job of pending.values()){clearTimeout(job.timer);job.reject(new Error(data.message));}pending.clear();}
 });
 showDetail();renderList();
 window.QibanAutoRead?.register('memory-refresh');window.QibanAutoRead?.register('fact-refresh');window.QibanAutoRead?.register('fact-retention-read',{dirty:()=>!!retention&&Number(el('fact-retention-days').value)!==retention.days});
})();
