"use strict";
const $ = (s) => document.querySelector(s), csrf = $('meta[name="csrf-token"]').content, kind = document.body.dataset.kind;
const notice = (t) => { $("#notice").textContent = t; };
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify(body)});
  const value = await response.json(); if (!response.ok) {if(response.status===401){$("#messages")?.replaceChildren();$("#task-list")?.replaceChildren();if($("#task-fields"))$("#task-fields").disabled=true;if($("#message button[type=submit]"))$("#message button[type=submit]").disabled=true;}throw new Error(value.message || value.error || "操作失败");} return value;
}
function bind(selector, callback, event="click") {
  const el=$(selector); if (el) el.addEventListener(event,async e=>{e.preventDefault();el.disabled=true;try{await callback(e);}catch(err){notice(err.message);}finally{el.disabled=false;}});
}
const setupTicket=location.hash.startsWith("#setup=")?location.hash.slice(7):"";
if(setupTicket) history.replaceState(null,"","/");
bind("#setup",async()=>{await api("/setup",{ticket:setupTicket,password:$('#setup input').value});location.reload();},"submit");
bind("#login",async()=>{await api("/login",{password:$('#login input').value});location.reload();},"submit");
bind("#logout",async()=>{await api("/logout",{});location.reload();});
let selected=null,next=null;
async function refresh(){
  const state=await api("/status");$("#status").textContent=state.status==="active"?(state.busy?"正在思考":"已启动"):"已暂停";$("#status").classList.toggle("active",state.status==="active");
  if(kind!=="role"){
    const backup=state.backup||{};
    if($('#backup-state'))$('#backup-state').textContent=(backup.status==='failed'?'最近一次备份未成功：'+backup.error:backup.last_saved_at?'最近成功备份：'+new Date(backup.last_saved_at*1000).toLocaleString():'等待首次自动备份')+(backup.capacity_warning?'；资料已接近应用备份容量上限，请安排受保护的 PostgreSQL 备份。':'')+(backup.recovery_key_exported?'；已导出过恢复密钥。':'；尚未导出恢复密钥，请在下方导出并单独保管。');
    const compression=state.compression||{},labels={idle:"尚未达到整理阈值",running:"正在整理较早的对话",saved:"最近一次整理已保存",deferred:"等待条件恢复后再整理",failed:"整理暂未成功，原文已保留"};
    if($('#compression-state'))$('#compression-state').textContent=(state.status!=="active"?"角色已暂停。 ":"")+(labels[compression.status]||"尚未开始整理")+(compression.last_saved_at?"；上次保存："+new Date(compression.last_saved_at*1000).toLocaleString():"");
    return;
  }$('#message button[type=submit]').disabled=state.status!=="active"||state.read_only;
  if($('#quota-hint'))$('#quota-hint').textContent='今日剩余模型调用 '+(state.quotas?.model.remaining??'—')+' 次，本机回复 '+(state.quotas?.local.remaining??'—')+' 次。额度不足时消息保留等待。';
  if(window.refreshTasks)await window.refreshTasks(state);
   if(window.refreshConversation)await window.refreshConversation();
}
const nonce=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,"0")).join("");
const operate=(operation,args={})=>api("/operate",{operation,arguments:args});
let baselinePreview=null,baselineCard=null;
async function loadBaseline(){
  baselinePreview=null;baselineCard=null;
  const preview=await operate("baseline_preview"),card=await api("/records?kind=persona&id=");
  if(preview.pins[0].version!==card.version)throw new Error("人物卡已变化，请重新读取");
  baselinePreview=preview;baselineCard=card;
  $('#baseline-state').textContent=preview.shared?"已启用：一张人物卡，每条回复完整加载 "+preview.foundation_count+" 条基础记忆。":"尚未登记。请查看人物卡和基础记忆，确认全部可公开后再启用。";
  $('#baseline-publish').hidden=preview.shared;$('#foundation-add').hidden=!preview.shared;
  const list=$('#baseline-records');list.replaceChildren();
  for(const pin of preview.pins){const row=document.createElement('div'),edit=document.createElement('button');edit.type='button';edit.textContent=pin.kind==='persona'?"编辑人物卡":pin.id;edit.addEventListener('click',async()=>{try{$('#kind').value=pin.kind;await selectRecord(pin.id||'@config');$('#editor').focus();}catch(e){notice(e.message);}});row.append(edit);
    if(preview.shared&&pin.kind==='memories'){const remove=document.createElement('button');remove.type='button';remove.className='quiet';remove.textContent='移出基础记忆';remove.addEventListener('click',async()=>{remove.disabled=true;try{await operate('config_patch',{kind:'persona',changes:{foundation_memories:card.data.foundation_memories.filter(id=>id!==pin.id)},expected_version:card.version});await loadBaseline();notice('已移出基础记忆，停止召回；历史版本仍可恢复。');}catch(e){notice(e.message);}finally{remove.disabled=false;}});row.append(remove);}list.append(row);}
}
bind('#baseline-load',loadBaseline);
bind('#baseline-publish',async()=>{if(!baselinePreview)throw new Error('请先读取并查看设定');if(!confirm('人物卡及清单内基础记忆将用于所有公开和私密回复。确认全部内容均可公开？'))return;const {shared,...expected}=baselinePreview;await operate('baseline_publish',{expected});await loadBaseline();notice('共用设定已登记，角色保持当前状态。');});
bind('#foundation-add',async()=>{if(!baselineCard)throw new Error('请先读取设定');await operate('foundation_add',{id:$('#foundation-id').value,content:$('#foundation-content').value,expected_version:baselineCard.version});$('#foundation-id').value='';$('#foundation-content').value='';await loadBaseline();notice('基础记忆已添加。');},'submit');
let quotaRecord=null,restoreToken=null;
const fieldLabels={daily_videos:"每日阅读视频",daily_model_cycles:"每日模型调用",daily_local_replies:"每日本机回复",daily_action_limit:"每日平台互动",backup_interval_minutes:"自动备份间隔（分钟）",cycle_minutes:"浏览周期间隔（分钟）",min_action_interval_seconds:"互动间隔（秒）",max_reply_turns_per_uid_day:"每人每日回复轮数"};
const actionLabels={comment:"发表评论",reply:"回复评论",comment_like:"评论点赞",dm_reply:"回复私信",dynamic:"发布动态",like:"视频点赞",favorite:"收藏",follow:"关注",coin:"投币"};
bind("#quota-load",async()=>{quotaRecord=await api("/records?kind=policy&id=");$("#quota-fields").replaceChildren();const fields=quotaRecord.data;
  function add(name,label,value,type="number"){const wrap=document.createElement("label");wrap.textContent=label;const input=document.createElement("input");input.name=name;input.type=type;if(type==="checkbox")input.checked=value;else input.value=value;if(type==="number")input.min="0";wrap.append(input);$("#quota-fields").append(wrap);}
  for(const [key,label] of Object.entries(fieldLabels)){const value=fields[key]??(key==='daily_local_replies'?fields.daily_model_cycles:key==='backup_interval_minutes'?60:undefined);if(value!==undefined)add(key,label,value);}
  if(fields.active_hours){add("start","活动开始",fields.active_hours.start,"time");add("end","活动结束",fields.active_hours.end,"time");}
  for(const [key,setting] of Object.entries(fields.actions||{})){add("enabled:"+key,"允许"+(actionLabels[key]||key),setting.enabled,"checkbox");add("limit:"+key,(actionLabels[key]||key)+"每日额度",setting.daily_limit);}
});
bind("#quota",async()=>{if(!quotaRecord)throw new Error("先读取当前设置");const patch={},actions=structuredClone(quotaRecord.data.actions||{}),hours=structuredClone(quotaRecord.data.active_hours||{});for(const input of $('#quota-fields').querySelectorAll('input')){if(input.name.startsWith('enabled:'))actions[input.name.slice(8)].enabled=input.checked;else if(input.name.startsWith('limit:'))actions[input.name.slice(6)].daily_limit=Number(input.value);else if(input.type==='time')hours[input.name]=input.value;else patch[input.name]=Number(input.value);}if(Object.keys(actions).length)patch.actions=actions;if(Object.keys(hours).length)patch.active_hours=hours;quotaRecord=await operate("config_patch",{kind:"policy",changes:patch,expected_version:quotaRecord.version});notice("日常设置已保存，角色运行状态未改变。");},"submit");
bind("#backup-list",async()=>{const files=await operate("backup_list");$('#restore-file').replaceChildren();for(const file of files){const option=document.createElement('option');option.value=file.name;option.textContent=file.name;$('#restore-file').append(option);}});
bind("#restore-preview",async()=>{const preview=await operate("restore_preview",{name:$('#restore-file').value});restoreToken=preview.token;const labels={content_records:"内容资料",content_history:"历史版本",core_events:"核心收件",core_actions:"核心动作",governance_grants:"跨账号授权",relationship_claims:"用户关系"};$('#restore-details').textContent=["备份："+preview.name,...Object.entries(labels).map(([key,label])=>label+"："+(preview.counts[key]||0)+" 条"),preview.effect].join("\n");notice("角色已暂停。请核对备份和覆盖范围，预览两分钟内有效。");});
bind("#restore",async()=>{if(!restoreToken)throw new Error("请先查看恢复预览");if(!confirm("将以该备份覆盖当前资料，撤销全部网页会话和跨账号授权。确认继续？"))return;await operate("restore",{token:restoreToken,password:$('#restore input').value});location.reload();},"submit");
bind("#pause",async()=>{await operate("pause");notice("角色已暂停，重新启动需要 Codex 的明确指令。");await refresh();});
bind("#backup",async()=>{notice("加密备份已保存："+JSON.stringify(await operate("backup")));});
bind("#recovery-key",async()=>{const input=$('#recovery-key input');const response=await fetch("/recovery-key",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify({password:input.value})});input.value="";if(!response.ok)throw new Error("恢复密钥导出失败，请检查登录及口令长度");const url=URL.createObjectURL(await response.blob()),link=document.createElement("a");link.href=url;link.download="recovery-key.cckey";link.click();URL.revokeObjectURL(url);notice("已导出加密恢复密钥。请与加密备份分别保存，并保管口令。");},"submit");
async function selectRecord(id){const type=$("#kind").value;selected={kind:type,id:id==="@config"?null:id};const value=await api("/records?kind="+encodeURIComponent(type)+"&id="+encodeURIComponent(selected.id||""));selected.value=value;$("#record-title").textContent=type+" / "+id+" · "+value.version.slice(0,12);$("#editor").value=JSON.stringify(value.data,null,2);$("#policy").textContent=JSON.stringify(value.policy,null,2);$("#classification").value=value.policy?.classification||"unclassified";$("#owners").value=(value.policy?.owners||[]).join(",");}
async function load(after=""){const value=await api("/records?kind="+encodeURIComponent($("#kind").value)+"&after="+encodeURIComponent(after));$("#records").replaceChildren();next=value.next;$("#next").hidden=!next;for(const r of value.records){const b=document.createElement("button");b.textContent=r.id;const s=document.createElement("small");s.textContent=r.classification||"待审核";b.append(s);b.addEventListener("click",()=>selectRecord(r.id).catch(e=>notice(e.message)));$("#records").append(b);}}
bind("#load",()=>load());bind("#next",()=>load(next));
bind("#save",async()=>{if(!selected?.value)throw new Error("请先选择资料");await operate("record_correct",{kind:selected.kind,id:selected.id,data:JSON.parse($("#editor").value),expected_version:selected.value.version});await selectRecord(selected.id||"@config");notice("修改已保存；依赖旧版本的派生内容需重新复核。");});
bind("#classify",async()=>{if(!selected?.value?.policy)throw new Error("请先选择资料");const p=selected.value.policy;if($("#classification").value==="public"&&!confirm("公开资料可以进入任意平台的角色回复。确认这条资料不含个人或敏感信息？"))return;await operate("classify",{resource:p.resource,expected:p.version,classification:$("#classification").value,owners:$("#owners").value.split(",").map(s=>s.trim()).filter(Boolean),conversation:p.conversation,audience:p.audience,sources:p.sources,valid:p.valid});await selectRecord(selected.id||"@config");notice("权限标记已更新。");});
bind("#export",async()=>{if(!selected)throw new Error("请先选择资料");const response=await fetch("/export",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify({kind:selected.kind,id:selected.id})});if(!response.ok)throw new Error("导出失败，请检查登录状态");const url=URL.createObjectURL(await response.blob()),link=document.createElement("a");link.href=url;link.download="record.yaml";link.click();URL.revokeObjectURL(url);});
bind("#import-yaml",async()=>{if(!selected?.value)throw new Error("请先选择要修改的资料");const file=$('#yaml-file').files[0];if(!file||file.size>24000)throw new Error("请选择不超过 24 KB 的 YAML 文件");const target=selected;const preview=await operate("yaml_preview",{kind:target.kind,id:target.id,text:await file.text(),expected_version:target.value.version});if(selected!==target)throw new Error("选择已变化，请重新预览");$('#editor').value=JSON.stringify(preview.data,null,2);notice("已载入预览，尚未写入。核对后点击保存修改；资料权限保持原设置。");});
bind("#actors",async()=>{$("#actor-list").textContent=JSON.stringify(await api("/actors"),null,2);});
bind("#grant",async()=>{notice("管理操作已完成："+JSON.stringify(await operate($("#grant-operation").value,JSON.parse($("#grant-arguments").value))));});
if(document.body.dataset.authenticated==="true"){refresh().catch(e=>notice(e.message));const poll=async()=>{try{if(!document.hidden)await refresh();}catch(e){notice(e.message);}finally{setTimeout(poll,3000);}};setTimeout(poll,3000);}
