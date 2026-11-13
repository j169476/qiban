// Reply processing trace page. Read-only: it lists the newest records and shows one
// record's stages and sources, and it never asks the backend for anything else.
(()=>{
 'use strict';
 const el=id=>document.getElementById('reply-trace-'+id),pending=new Map();
 const PLATFORM={qq:'QQ',wechat:'微信',bilibili:'B站',local:'本机会客室'};
 const STATE={completed:'完成',failed:'失败',cancelled:'取消',running:'处理中'};
 const STAGE={model_selection:'选择模型',context:'组装上下文','recall.expand':'扩展检索',
  query_plan:'检索路线',backlog_context:'积压对话',context_budget:'上下文预算',
  'reply.generate':'生成回复','reply.summarize':'摘要积压对话',outcome:'处理结果',
  memory_commit:'写入记忆',facts_commit:'记录个人事实'};
 const FIELD={observation_id:'观测编号',kind:'记录种类',status:'结果',duration_ms:'用时',
  error_code:'错误标签',input_tokens:'输入用量',cached_input_tokens:'缓存输入用量',
  output_tokens:'输出用量',reasoning_output_tokens:'推理输出用量',usage_events:'用量事件',
  requested_model:'请求模型',effort:'推理强度',memory_records:'可用记忆条数',
  catalog_records:'记忆目录条数',recent_turns:'最近对话条数',recalled_turns:'召回对话条数',
  summary_present:'含对话摘要',conversation_chars:'对话字符数',coverage:'覆盖情况',
  route:'检索路线',reason:'原因',expansion_count:'扩写问题数',memories:'写入记忆数',facts:'写入事实数'};
 let allowed=false,busy=false,epoch=0,detailToken=0,rows=null,selected=null,record=null;
 const notes={panel:'',detail:''};
 function say(place,text){notes[place]=text;}
 // Any value that is not a plain scalar is shown as text, never as markup.
 function valueText(value){
  if(value===null)return '无';
  if(typeof value==='boolean')return value?'是':'否';
  if(typeof value==='number')return Number.isFinite(value)?String(value):'不是有效数字';
  if(typeof value==='string')return value;
  try{const text=JSON.stringify(value);return typeof text==='string'?text:'无法显示的内容';}
  catch(error){return '无法显示的内容';}
 }
 function timeText(value){
  if(typeof value!=='string'||!value)return '时间不可读';
  const date=new Date(value);
  return Number.isNaN(date.getTime())?'时间不可读':date.toLocaleString('zh-CN',{hour12:false});
 }
 function endText(row){
  if(typeof row.started!=='string'||typeof row.duration_ms!=='number'||!Number.isFinite(row.duration_ms))
   return '结束时间不可读';
  const start=new Date(row.started);
  if(Number.isNaN(start.getTime()))return '结束时间不可读';
  return timeText(new Date(start.getTime()+row.duration_ms).toISOString());
 }
 function durationText(value){
  return typeof value==='number'&&Number.isFinite(value)&&value>=0?String(value)+' 毫秒':'用时未知';
 }
 // Unknown platform or state values stay readable instead of being guessed.
 function platformText(value){
  return typeof value==='string'&&PLATFORM[value]?PLATFORM[value]:valueText(value);
 }
 function stateText(value){
  return typeof value==='string'&&STATE[value]?STATE[value]:'未识别的状态（'+valueText(value)+'）';
 }
 function paragraph(parent,text,className){
  const node=document.createElement('p');
  if(className)node.className=className;
  node.textContent=text;parent.append(node);return node;
 }
 function feedback(parent,place){
  const node=document.createElement('p');node.className='note';node.setAttribute('role','status');
  node.textContent=notes[place]||'';parent.append(node);return node;
 }
 function stepBlock(step){
  const item=document.createElement('article');item.className='reply-trace-step';
  const value=step&&typeof step==='object'?step:{};
  const title=document.createElement('h5');
  const stage=value.stage;
  title.textContent=typeof stage==='string'?STAGE[stage]||stage:'未记录阶段名';
  item.append(title);
  const list=document.createElement('ul');
  for(const key of Object.keys(value)){
   if(key==='stage')continue;
   const row=document.createElement('li');
   row.textContent=(FIELD[key]||key)+'：'+valueText(value[key]);
   list.append(row);
  }
  if(!list.children.length){
   const empty=document.createElement('li');empty.textContent='这一阶段没有附加信息。';list.append(empty);
  }
  item.append(list);return item;
 }
 function renderStages(box,row){
  const steps=Array.isArray(row.steps)?row.steps:[];
  const head=document.createElement('h4');
  head.textContent='处理阶段（'+(steps.length?steps.length+' 个':'0 个')+'）';box.append(head);
  if(!steps.length){
   paragraph(box,'这条记录没有处理阶段信息，可能是程序刚写入记录或处理尚未开始。','note');return;
  }
  for(const step of steps)box.append(stepBlock(step));
 }
 function renderSources(box,row){
  const sources=Array.isArray(row.sources)?row.sources:[];
  const head=document.createElement('h4');
  head.textContent='本次使用的来源（当前仍可读 '+sources.length+' 个）';box.append(head);
  if(!sources.length){
   paragraph(box,'这条记录没有可显示的来源：可能是本次没有使用资料，也可能是资料已删除或当前权限不足。','note');
  }else{
   const list=document.createElement('ul');
   for(const source of sources){
    const item=document.createElement('li');
    const version=source&&source.version_current?'仍为当前版本':'已不是当前版本';
    item.textContent='资源：'+valueText(source&&source.resource)+'；版本：'+valueText(source&&source.version)+
     '；'+version;
    list.append(item);
   }
   box.append(list);
  }
  const hidden=typeof row.hidden_sources==='number'&&Number.isFinite(row.hidden_sources)?row.hidden_sources:0;
  if(hidden>0)paragraph(box,'另有 '+hidden+' 个来源按当前读权限不可读，已隐藏。','note');
  if(row.sources_limited)paragraph(box,'本次使用的来源超过 100 个，记录只保留了前 100 个（已截断）。','note');
  paragraph(box,'来源只显示资源标识和版本，本页不提供打开原文的入口。','note');
 }
 function renderDetail(){
  const box=el('detail');box.replaceChildren();
  feedback(box,'detail');
  if(!allowed||!record)return;
  const head=document.createElement('h3');
  head.textContent=timeText(record.started)+' · '+platformText(record.platform)+' · '+stateText(record.status);
  box.append(head);
  const running=record.status==='running';
  paragraph(box,'记录状态：'+stateText(record.status));
  paragraph(box,'平台：'+platformText(record.platform));
  paragraph(box,'开始时间：'+timeText(record.started));
  paragraph(box,'结束时间：'+(running?'仍在处理中，还没有结束时间':endText(record)));
  paragraph(box,'用时：'+(running?'仍在处理中':durationText(record.duration_ms)));
  if(record.error)paragraph(box,'失败或取消标签：'+valueText(record.error));
  renderStages(box,record);
  renderSources(box,record);
  if(typeof record.meaning==='string'&&record.meaning)paragraph(box,record.meaning,'note');
  paragraph(box,'记录标识：'+valueText(record.id),'note');
 }
 function renderList(){
  const box=el('list');box.replaceChildren();
  const head=document.createElement('h4');
  head.textContent=rows?('最近 '+rows.length+' 条记录'):'最近记录';box.append(head);
  if(!rows){paragraph(box,'还没有读取最近记录。','note');return;}
  if(!rows.length){
   paragraph(box,'本机还没有回复处理记录。程序处理一次回复后才会写入记录；更早的记录超过保留期后会被自动清理。','note');
   return;
  }
  for(const row of rows){
   const button=document.createElement('button');
   button.className='quiet';
   button.textContent=timeText(row.started)+' · '+platformText(row.platform)+' · '+stateText(row.status)+
    ' · '+durationText(row.duration_ms);
   button.disabled=!allowed;
   if(row.id===selected)button.setAttribute('aria-current','true');
   button.onclick=()=>void loadDetail(row.id);
   box.append(button);
  }
  paragraph(box,'列表只包含最近的若干条，没有翻页；需要更多请把“最近数量”调大。','note');
 }
 function render(){
  el('load').disabled=!allowed||busy;
  el('limit').disabled=!allowed||busy;
  el('status').textContent=notes.panel||'';
  renderList();renderDetail();
 }
 function reset(){
  epoch++;detailToken++;
  busy=false;rows=null;selected=null;record=null;say('panel','');say('detail','');render();
 }
 function setAllowed(value){
  if(value===allowed)return;
  allowed=value;reset();
 }
 // Dropping the shown record also retires any detail reply still in flight and its
 // loading note: without a new token an older response could paint back a record the
 // page just removed, and a discarded read would keep claiming it is still loading.
 function dropDetail(){
  selected=null;record=null;detailToken++;say('detail','');
 }
 function call(operation,args){
  const request_id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{
    pending.delete(request_id);
    reject(Error('操作超时，请重新读取回复处理记录。'));
   },15000);
   pending.set(request_id,{resolve,reject,timer});
   window.chrome.webview.postMessage({action:'management',operation,arguments:args,request_id});
  });
 }
 function limitValue(){
  const raw=el('limit').value;
  if(typeof raw!=='string'||!/^(0|[1-9][0-9]{0,8})$/.test(raw)){
   say('panel','最近数量必须填写 1 至 100 的整数，不能留空。');render();return null;
  }
  const value=Number(raw);
  if(value<1||value>100){
   say('panel','最近数量必须在 1 至 100 之间。');render();return null;
  }
  return value;
 }
 async function load(){
  if(!allowed||busy)return;
  const current=epoch,limit=limitValue();
  if(limit===null)return;
  busy=true;say('panel','正在读取最近记录……');render();
  try{
   const value=await call('reply_trace.list',{limit});
   if(epoch!==current)return;
   rows=Array.isArray(value.items)?value.items:[];
   say('panel','已读取最近 '+rows.length+' 条回复处理记录。');
   // A re-read may drop the selected record: the detail leaves the page and the reply
   // that is still on its way must not be able to paint it back afterwards.
   if(selected!==null&&!rows.some(row=>row.id===selected))dropDetail();
  }catch(error){
   if(epoch!==current)return;
   if(error.code==='desktop_locked'){setAllowed(false);return;}
   // A failed read clears the list, so no pending detail may repaint it either.
   rows=null;dropDetail();
   // The nearby button is the retry: failing must not look like an empty result.
   say('panel',(error.message||'最近记录未能读取。')+' 可以点“读取最近记录”重试。');
  }finally{
   if(epoch===current){busy=false;render();}
  }
 }
 async function loadDetail(identifier){
  if(!allowed||typeof identifier!=='string')return;
  // Every selection owns a fresh token, so an older reply can never overwrite it.
  const token=++detailToken;
  selected=identifier;record=null;say('detail','正在读取这条记录……');render();
  try{
   const value=await call('reply_trace.detail',{identifier});
   // reset() bumps the token too, so a lock during the read cannot repaint either.
   if(token!==detailToken)return;
   record=value;say('detail','');
  }catch(error){
   if(token!==detailToken)return;
   if(error.code==='desktop_locked'){setAllowed(false);return;}
   record=null;
   // "不存在或已过期" is reported as a failure, never as a loaded record.
   say('detail',error.message||'这条记录未能读取，请重新选择。');
  }
  if(token===detailToken)render();
 }
 el('load').onclick=()=>(allowed&&!busy)?load():undefined;
 window.addEventListener('qiban-management-state',({detail})=>{
  el('panel').hidden=document.getElementById('management-panel').hidden;
  const now=detail.ready&&!detail.locked;
  if(now===allowed)return;
  allowed=now;reset();
  if(allowed)void load();
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
 if(!el('limit').value)el('limit').value='30';
 render();
})();
