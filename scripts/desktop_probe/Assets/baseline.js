(()=>{
 'use strict';
 const el=id=>document.getElementById(id),pending=new Map();
 let allowed=false,busy=false,epoch=0,snapshot=null,dirty=false;
 const names={name:'名字',id:'资料标识',core:'基础人设',description:'描述',speaking_style:'说话方式',interests:'兴趣',locked_fields:'禁止自动修改的字段',relationship_baseline:'基础关系设定',owner_uid:'关联账号',owner_name:'关联称呼',addresses:'称呼方式',level:'关系',source:'来源',type:'来源类型',recorded_at:'记录时间',reference:'来源说明',visual_identity:'外观设定',references:'参考素材',filename:'素材名称',sha256:'素材校验标识',label:'说明',foundation_memories:'基础记忆清单',kind:'记忆类型',content:'正文',confidence:'可信程度',tags:'标签',locked:'禁止自动修改',fact:'事实资料'};
 function call(operation,args={}){
  const id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(id);reject(new Error('操作未及时返回，请重新读取确认结果。'));},15000);
   pending.set(id,{resolve,reject,timer});window.chrome.webview.postMessage({action:'management',operation,arguments:args,request_id:id});
  });
 }
 const notice=text=>{el('baseline-notice').textContent=text;};
 function controls(){
  const ready=allowed&&!busy;
  el('baseline-refresh').disabled=!ready||dirty;
  el('baseline-confirm').disabled=!ready||!snapshot||snapshot.preview.shared;
  el('baseline-publish').disabled=!ready||!snapshot||snapshot.preview.shared||!el('baseline-confirm').checked;
  el('baseline-publish-area').hidden=!!snapshot?.preview.shared;
  el('foundation-area').hidden=!snapshot?.preview.shared;
  el('foundation-content').disabled=!ready||!snapshot?.preview.shared;
  el('foundation-confirm').disabled=!ready||!snapshot?.preview.shared;
  el('foundation-add').disabled=!ready||!snapshot?.preview.shared||!dirty||!el('foundation-confirm').checked;
  el('foundation-discard').disabled=!ready||!dirty;
  for(const button of el('baseline-records').querySelectorAll('button'))button.disabled=!ready||dirty;
  el('foundation-draft').textContent=dirty?'有未保存的基础记忆。添加或放弃后可重新读取。':'';
 }
 function resetConfirm(){el('baseline-confirm').checked=false;el('foundation-confirm').checked=false;}
 const values={kind:{user_preference:'偏好',experience:'经历',reflection:'思考'},confidence:{explicit:'明确告知',observed:'观察所得',inferred:'推断'},type:{user_instruction:'用户说明',conversation:'对话',video:'视频',interaction:'互动',web:'网页'}};
 function describe(value,field=''){
  if(value===null)return '未设置';
  if(typeof value==='boolean')return value?'是':'否';
  if(Array.isArray(value))return value.length?value.map(item=>field==='locked_fields'?(names[item]||item):describe(item,field)).join('\n'):'未设置';
  if(typeof value==='object')return Object.entries(value).filter(([key])=>key!=='schema_version').map(([key,item])=>`${key==='locked'?'单条记忆锁定标记':names[key]||key}：${describe(item,key)}`).join('\n');
  if(values[field]?.[value])return values[field][value];
  return String(value)||'未设置';
 }
 function render(){
  const list=el('baseline-records');list.replaceChildren();
  el('baseline-state').textContent=!snapshot?'请重新读取共用设定。':snapshot.preview.shared?`已启用共用设定，包含 ${snapshot.preview.foundation_count} 条基础记忆。`:'尚未启用。确认下方内容适合公开使用后再启用。';
  for(const item of snapshot?.records||[]){
   const card=document.createElement('article'),title=document.createElement('h3'),text=document.createElement('p');
   title.textContent=item.kind==='persona'?'人物卡':`基础记忆 · ${item.data.id}`;
   text.className='baseline-content';text.textContent=describe(item.data);card.append(title,text);
   const protection=document.createElement('p');
   protection.textContent=snapshot.preview.shared?'只能由管理员修改，角色不会自动修改。':'启用后，这些内容只能由管理员修改。';
   card.append(protection);
   if(item.kind==='memories'&&snapshot.preview.shared){
    const button=document.createElement('button');button.className='secondary';button.textContent='移出共用设定';
    button.onclick=()=>{
     if(dirty||!snapshot)return;
     const card=snapshot.records[0];
     return modify('config.patch',{kind:'persona',expected:card.version,changes:{foundation_memories:card.data.foundation_memories.filter(id=>id!==item.data.id)}},'已移出共用设定；原记录与历史保留。');
    };card.append(button);
   }
   list.append(card);
  }
  controls();
 }
 async function load(live){
  const preview=await call('baseline.preview'),records=[];
  for(const pin of preview.pins){
   if(!live())return;
   const item=await call('records.read',{kind:pin.kind,identifier:pin.id});
   if(item.version!==pin.version||item.policy?.version!==pin.policy_version)throw new Error('设定已发生变化，请重新读取后确认。');
   records.push({...item,kind:pin.kind});
  }
  if(live()){snapshot={preview,records};resetConfirm();render();}
 }
 async function run(work){
  if(!allowed||busy)return;busy=true;const token=epoch,live=()=>allowed&&epoch===token;controls();
  try{await work(live);}catch(error){if(live())notice(error.message);}
  finally{if(live()){busy=false;controls();}}
 }
 function modify(operation,args,message){
  return run(async live=>{
   await call(operation,args);if(!live())return;
   dirty=false;el('foundation-content').value='';snapshot=null;resetConfirm();render();
   try{await load(live);if(live())notice(message);}
   catch{if(live())notice(message+' 列表暂未更新，请重新读取。');}
  });
 }
 el('baseline-refresh').onclick=()=>{if(dirty)return;return run(async live=>{snapshot=null;resetConfirm();render();await load(live);if(live())notice('共用设定已读取。');});};
 el('baseline-confirm').onchange=controls;el('foundation-confirm').onchange=controls;
 el('baseline-publish').onclick=()=>{
  if(!snapshot||snapshot.preview.shared||!el('baseline-confirm').checked)return;
  const {pins,foundation_count}=snapshot.preview;
  return modify('baseline.publish',{expected:{pins,foundation_count}},'共用设定已启用。');
 };
 el('foundation-content').oninput=()=>{dirty=!!el('foundation-content').value;el('foundation-confirm').checked=false;notice('');controls();};
 el('foundation-discard').onclick=()=>{if(busy)return;dirty=false;el('foundation-content').value='';resetConfirm();notice('已放弃未保存的基础记忆。');controls();};
 el('foundation-add').onclick=()=>{
  if(!snapshot?.preview.shared||!dirty||!el('foundation-confirm').checked)return;
  const content=el('foundation-content').value;
  if(!content.trim()||content.length>16000){notice('请输入1至16000字的基础记忆。');return;}
  return modify('foundation.add',{identifier:'foundation-'+crypto.randomUUID().replaceAll('-',''),content,expected:snapshot.records[0].version},'基础记忆已添加。');
 };
 window.addEventListener('qiban-management-state',({detail})=>{
  const value=detail.ready&&!detail.locked,changed=value!==allowed;allowed=value;el('baseline-panel').hidden=el('management-panel').hidden;
  if(!changed)return;
  if(!allowed){epoch++;busy=false;snapshot=null;dirty=false;el('foundation-content').value='';resetConfirm();render();notice('');}
  else void run(load);
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management'){
   const job=pending.get(data.request_id);if(!job)return;clearTimeout(job.timer);pending.delete(data.request_id);
   if(data.response.ok)job.resolve(data.response.result);else job.reject(new Error(data.response.message||'共用设定操作未完成。'));
  }
  if(data.type==='management_error'){for(const job of pending.values()){clearTimeout(job.timer);job.reject(new Error(data.message));}pending.clear();}
 });
 render();
 window.QibanAutoRead?.register('baseline-refresh',{dirty:()=>dirty});
})();
