(()=>{
 'use strict';
 const el=id=>document.getElementById('relation-'+id), pending=new Map();
 const fields=['name','addresses','level','description'];
 let allowed=false,busy=false,epoch=0,snapshot=null,accounts=[],members=[],selected=new Set(),anchor='',next=null,dirty=false;
 let linkRows=[],linkNext=null,linkDetail=null;
 const label=a=>({qq:'QQ',wechat:'微信',bilibili:'B站'}[a.platform]||a.platform)+' · '+a.user_id+(a.scope?'（接入账号：'+a.scope+'）':'');
 const notice=text=>{el('notice').textContent=text;};
 function call(op,args={}){
  const request_id=crypto.randomUUID().replaceAll('-','');
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(request_id);reject(Error('未及时返回，请重新读取核对结果。'));},15000);
   pending.set(request_id,{resolve,reject,timer});window.chrome.webview.postMessage({action:'management',operation:'relationship.'+op,arguments:args,request_id});
  });
 }
 let preview=null;
 function closePreview(){
  preview=null;
  el('link-preview').hidden=true;
  el('groups').textContent='';
 }
 function fillPicker(id,items){
  const box=el(id),previous=box.value;
  box.replaceChildren();
  const placeholder=document.createElement('option');
  placeholder.value='';placeholder.textContent='选择已登记账号';box.append(placeholder);
  for(const item of items){
   const option=document.createElement('option');
   option.value=item.key;option.textContent=label(item);box.append(option);
  }
  box.value=items.some(item=>item.key===previous)?previous:'';
 }
 function linkPickers(){
  fillPicker('left',accounts);
  const first=accounts.find(item=>item.key===el('left').value);
  fillPicker('right',first?accounts.filter(item=>item.platform!==first.platform):[]);
 }
 function controls(){
  el('links').hidden=!snapshot;
  el('left').disabled=busy;el('right').disabled=busy||!el('left').value;
  el('review').disabled=busy||!el('right').value;
  el('confirm').disabled=busy||!preview;el('cancel').disabled=busy;
  if(dirty)el('clear-preview').hidden=true;
  el('read').disabled=!allowed||busy||dirty;el('form').hidden=!snapshot?.shared;
  el('clear').hidden=!snapshot?.setting;el('clear').disabled=busy||dirty;el('clear-confirm').disabled=busy||dirty;
  el('more').hidden=!next;el('more').disabled=busy;el('anchor').disabled=busy;
  for(const id of fields)el(id).disabled=busy||!anchor;
  for(const box of el('members').querySelectorAll('input'))box.disabled=busy||box.value===anchor;
  el('save').disabled=busy||!allowed||!dirty||!selected.has(anchor);
  el('discard').disabled=busy||!dirty;
  const ready=allowed&&!busy;
  el('link-list').disabled=!ready;el('link-more').disabled=!ready;el('link-more').hidden=!linkNext;
  el('link-revoke').disabled=!ready||!linkDetail||!linkDetail.revocable;
  el('link-revoke-cancel').disabled=!ready;el('link-revoke-reason').disabled=!ready;
  for(const button of el('link-items').children)button.disabled=!ready;
 }
 function options(){
  linkPickers();const box=el('anchor');box.replaceChildren();
  const option=(key,text)=>{const item=document.createElement('option');item.value=key;item.textContent=text;box.append(item);};
  option('','选择一个已登记的平台账号');
  const lookup=new Map([...accounts,...members].map(item=>[item.key,item]));
  if(anchor&&!lookup.has(anchor))option(anchor,'当前主要账号（待核对）');
  for(const item of lookup.values())option(item.key,label(item));box.value=anchor;
 }
 function checks(){
  const list=el('members');list.replaceChildren();
  for(const item of members){
   const row=document.createElement('label'),box=document.createElement('input');box.type='checkbox';box.value=item.key;box.checked=selected.has(item.key);
   box.onchange=()=>{if(box.checked)selected.add(item.key);else selected.delete(item.key);dirty=true;notice('有未保存的基础关系更改。');controls();};
   row.append(box,document.createTextNode(label(item)));list.append(row);
  }controls();
 }
 function line(box,text){const node=document.createElement('p');node.textContent=text;box.append(node);return node;}
 function heading(box,text){const node=document.createElement('h4');node.textContent=text;box.append(node);return node;}
 const origin=row=>row.status==='revoked'?'记录保留':row.manual?'管理员处理':'模型推断';
 function renderLinks(){
  const box=el('link-items');box.replaceChildren();
  for(const row of linkRows){
   const button=document.createElement('button');button.className='secondary';
   button.textContent=label(row.left)+' ⇄ '+label(row.right)+' · '+row.status_label+' · '+origin(row);
   button.onclick=()=>work(async live=>{await openLink(row.id,live);});
   box.append(button);
  }controls();
 }
 function renderDetail(){
  const box=el('link-detail');box.replaceChildren();
  el('link-revoke-area').hidden=true;el('link-revoke-reason').value='';el('link-revoke-notice').textContent='';
  if(!linkDetail){controls();return;}
  const value=linkDetail;
  line(box,'关联：'+label(value.left)+' ⇄ '+label(value.right));
  line(box,'当前状态：'+value.status_label+' · '+origin(value)+(value.locked?' · 已锁定':''));
  line(box,'原因：'+(value.reason||'未记录'));
  heading(box,'保存的证据引用（当时的引文与原版本，不读其他消息）');
  if(!value.evidence.items.length)line(box,'这条关联没有保存证据引用。');
  for(const item of value.evidence.items)
   line(box,(item.quote?'「'+item.quote+'」':'（正文未展示）')+' · 引用版本 '+String(item.stored_version||'').slice(0,12)+'… · '+item.state_label);
  line(box,value.evidence.meaning);
  heading(box,'当前同一人成员（按现有身份图读取）');
  line(box,value.person.accounts.map(label).join('、')+'（共'+value.person.count+'个）');
  line(box,value.person.notice);
  heading(box,'最近变更（文档内最多保留最近100条，不是全部历史）');
  if(!value.history.items.length)line(box,'这条关联没有文档内变更记录。');
  for(const entry of value.history.items)
   line(box,[entry.at||'时间未记录',entry.status_label,entry.kind,entry.reason,entry.evidence_count?'引用'+entry.evidence_count+'条':''].filter(Boolean).join(' · '));
  line(box,value.history.meaning);
  el('link-revoke-notice').textContent=value.notice;
  el('link-revoke-area').hidden=!value.revocable;
  controls();
 }
 async function loadLinks(after,live){
  const page=await call('links',{after});if(!live())return;
  linkRows=after?[...linkRows,...page.links]:page.links;linkNext=page.next;
  el('link-list-status').textContent=linkRows.length?('已读取'+linkRows.length+'条关联，共'+page.total+'条。'):'目前没有保存的账号关联。';
  renderLinks();
 }
 async function openLink(identifier,live){
  const value=await call('link_detail',{identifier});if(!live())return;
  linkDetail=value;renderDetail();
 }
 async function refreshMembers(live){
  if(!anchor)return true;
  try{
   const group=await call('person',{anchor});if(!live())return false;
   members=group.members;selected=new Set([...selected].filter(key=>members.some(item=>item.key===key)));
   options();checks();return true;
  }catch(error){return false;}
 }
 function reset(){closePreview();el('link-notice').textContent='';el('clear-preview').hidden=true;snapshot=null;accounts=[];members=[];selected=new Set();anchor='';next=null;dirty=false;
  linkRows=[];linkNext=null;linkDetail=null;el('link-items').replaceChildren();el('link-detail').replaceChildren();
  el('link-list-status').textContent='';renderDetail();
  el('status').textContent='';for(const id of fields)el(id).value='';options();checks();notice('');}
 async function work(job){if(!allowed||busy)return;const token=epoch,live=()=>allowed&&token===epoch;busy=true;controls();
  try{await job(live);}catch(err){if(live())notice(err.message);}finally{if(live()){busy=false;controls();}}}
 async function load(live){
  el('clear-preview').hidden=true;const data=await call('read');if(!live())return;
  closePreview();el('link-notice').textContent='';const page=await call('accounts',{after:''});if(!live())return;
  let group={members:[]};if(data.anchor){try{group=await call('person',{anchor:data.anchor});}catch(err){if(err.code!=='unknown_actor')throw err;}}if(!live())return;
  snapshot=data;accounts=page.accounts;next=page.next;anchor=data.anchor||'';members=group.members;
  selected=new Set(data.selected.filter(key=>members.some(x=>x.key===key)));dirty=false;
  const s=data.setting||{};el('name').value=s.owner_name||'';el('addresses').value=(s.addresses||[]).join('\n');
  el('level').value=s.level||'';el('description').value=s.description||'';
  el('status').textContent=!data.shared?'请先启用共用人物设定。':data.selected.length>selected.size?'部分原账号的身份关联已失效，不再匹配；保存前请重新核对。':s.owner_uid?'当前使用旧版B站关系；选择主要账号并保存后改用统一人物关系。':s.target?'已保存一份基础关系。':accounts.length?'尚未设置基础关系。':'暂无已登记账号。收到平台消息并识别发送者后可在这里选择。';
  options();checks();
 }
 for(const id of ['left','right'])el(id).onchange=()=>{
  closePreview();el('link-notice').textContent='';linkPickers();controls();
 };
 el('cancel').onclick=()=>{closePreview();controls();};
 el('review').onclick=()=>work(async live=>{
  closePreview();
  const left=el('left').value,right=el('right').value;
  if(!left||!right)return;
  try{
   const result=await call('link_preview',{left,right});if(!live())return;
   preview={left,right,expected:result.expected};
   el('groups').textContent=result.groups.map(group=>group.map(label).join('、')).join(' 与 ')+
    ' 将在所有平台归为同一人。基础关系的适用账号需另行勾选。';
   el('link-notice').textContent='';el('link-preview').hidden=false;
  }catch(err){if(live())el('link-notice').textContent=err.message;}
 });
 el('confirm').onclick=()=>work(async live=>{
  if(!preview)return;
  const args=preview;closePreview();
  try{await call('link_confirm',args);}catch(err){if(live())el('link-notice').textContent=err.message;return;}
  if(!live())return;
  el('link-notice').textContent='已确认是同一人。';
  if(!anchor)return;
  if(!await refreshMembers(live)&&live())el('link-notice').textContent='关联已保存。请重新读取适用账号。';
 });
 el('link-list').onclick=()=>work(async live=>{
  linkRows=[];linkNext=null;linkDetail=null;renderDetail();
  await loadLinks('',live);
 });
 el('link-more').onclick=()=>work(async live=>{if(linkNext)await loadLinks(linkNext,live);});
 el('link-revoke-cancel').onclick=()=>{
  if(busy)return;
  linkDetail=null;el('link-revoke-area').hidden=true;el('link-revoke-reason').value='';
  el('link-revoke-notice').textContent='';el('link-detail').replaceChildren();controls();
  notice('已收起撤销确认，没有写入任何更改。');
 };
 el('link-revoke').onclick=()=>work(async live=>{
  if(!linkDetail||!linkDetail.revocable)return;
  const target=linkDetail.id,args={identifier:target,expected:linkDetail.version,reason:el('link-revoke-reason').value.trim()};
  linkDetail=null;el('link-revoke-area').hidden=true;controls();
  try{await call('link_revoke',args);}
  catch(error){
   if(!live())return;
   notice(error.message+' 本页不会自动重试，请重新读取这条关联后再决定。');
   try{await openLink(target,live);}catch(inner){if(live())notice(error.message);}
   return;
  }
  if(!live())return;
  el('link-revoke-reason').value='';
  notice('已撤销这条账号关联（已锁定，记录与历史保留）。其他关联路径仍可能使这些账号被视作同一人；共用关系设定按原规则动态匹配，请重新核对适用账号。');
  linkRows=[];linkNext=null;
  await loadLinks('',live);if(!live())return;
  await openLink(target,live);if(!live())return;
  if(anchor&&!await refreshMembers(live)&&live())notice('撤销已保存，但适用账号未能重新读取；请点“读取基础关系”重新核对。');
 });
 el('clear').onclick=()=>{if(!dirty&&!busy&&snapshot?.setting)el('clear-preview').hidden=false;};
 el('clear-cancel').onclick=()=>{el('clear-preview').hidden=true;};
 el('clear-confirm').onclick=()=>work(async live=>{
  if(dirty||el('clear-preview').hidden||!snapshot?.setting)return;
  await call('clear',{expected:snapshot.expected});if(!live())return;
  reset();notice('已解除手工基础关系；账号关联和互动历史保留。');
 });
 el('read').onclick=()=>{if(!dirty)return work(async live=>{await load(live);if(live())notice('基础关系已读取。');});};
 el('more').onclick=()=>work(async live=>{if(!next)return;const page=await call('accounts',{after:next});if(!live())return;accounts.push(...page.accounts);next=page.next;options();});
 el('anchor').onchange=()=>work(async live=>{
  const key=el('anchor').value;try{const group=key?await call('person',{anchor:key}):{members:[]};if(!live())return;
   anchor=key;members=group.members;selected=new Set(key?[key]:[]);dirty=true;options();checks();notice('请核对适用账号，其他关联账号不会自动勾选。');
  }catch(error){if(live())el('anchor').value=anchor;throw error;}
 });
 for(const id of fields)el(id).oninput=()=>{dirty=true;notice('有未保存的基础关系更改。');controls();};
 el('discard').onclick=()=>work(async live=>{await load(live);if(live())notice('已重新读取，未保存的更改已放弃。');});
 el('save').onclick=()=>work(async live=>{
  if(!dirty||!snapshot||!selected.has(anchor))return;
  const addresses=el('addresses').value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean);
  const setting={owner_name:el('name').value.trim(),addresses,level:el('level').value.trim(),description:el('description').value};
  if(!setting.owner_name||!setting.level||!addresses.length||addresses.length>10||addresses.some(v=>v.length>100)){notice('请填写人物名称、关系和1到10个称呼，每个称呼最多100字。');return;}
  const res=await call('save',{expected:snapshot.expected,anchor,selected:[...selected],setting});if(!live())return;
  snapshot={...snapshot,expected:res.version,anchor,selected:[...selected],setting};dirty=false;el('status').textContent='已保存一份基础关系。';notice('基础关系已保存，适用于'+res.selected_count+'个已确认账号。');
 });
 window.addEventListener('qiban-management-state',({detail})=>{
  const now=detail.ready&&!detail.locked;document.getElementById('relationship-panel').hidden=document.getElementById('management-panel').hidden;
  if(now!==allowed){allowed=now;epoch++;busy=false;reset();}controls();
 });
 window.chrome.webview.addEventListener('message',({data})=>{
  if(data.type==='management'){const p=pending.get(data.request_id);if(!p)return;pending.delete(data.request_id);clearTimeout(p.timer);
   if(data.response.ok)p.resolve(data.response.result);else p.reject(Object.assign(Error(data.response.message||'基础关系操作失败。'),{code:data.response.error}));}
  if(data.type==='management_error'){for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error(data.message));}pending.clear();}
 });
 reset();
 window.QibanAutoRead?.register('relation-read',{dirty:()=>dirty});
})();
