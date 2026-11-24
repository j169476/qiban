/* Group participation and reminders use the authenticated desktop management bridge. */
(() => {
 'use strict';
 const {ManagementList,element,button,detailRow,localTime}=window.QibanDesktopLists;
 function checkbox(text,checked){
  const label=element('label',null,'probe-check'),input=element('input');
  input.type='checkbox';input.checked=checked;label.append(input,text);return {label,input};
 }

 const platformNames={qq:'QQ',wechat:'微信',local:'会客室',bilibili:'B 站'};
 const accounts={qq:null,wechat:null};
 const groups=new ManagementList('group-modes-panel','group_modes.list',groupRow);
 const reminders=new ManagementList('reminders-panel','reminders.list',reminderRow);
 const groupAccount=groups.root.querySelector('[data-scope]');
 const allGroups=groups.root.querySelector('[data-all]');
 const reminderFilter=reminders.root.querySelector('[data-filter]');
 const delivery=reminders.root.querySelector('[data-delivery]');
 reminders.onPage=page=>{
  const rows=Object.entries(page?.capabilities||{}).map(([key,modes])=>{
   const kinds=[modes.private&&'私聊',modes.group&&'群聊'].filter(Boolean);
   return element('li',(platformNames[key]||key)+' · '+(kinds.join('、')||'暂不支持'));
  });
  delivery.hidden=rows.length===0;
  if(!rows.length)delivery.open=false;
  delivery.querySelector('ul').replaceChildren(...rows);
 };
 let session=null,selectedGroup=null,lastPlatform=null;

 function groupRow(row,list){
  const name=row.name||row.target;
  const capabilities=row.capabilities||{};
  const direct=row.excluded?'会话已停用':row.reply_authorized?'被 @ 或被引用时可回复':'被 @ 或被引用时无回复授权';
  const status=row.excluded?direct:[direct,!capabilities.listen?'旁听不可用':row.listen?'旁听已开':'旁听已关',
   !capabilities.proactive?'主动参与不可用':row.proactive?(row.reply_authorized?'主动参与已开':'主动参与缺少回复授权'):'主动参与已关'].join(' · ');
  const {details,fields}=detailRow(name,status);
  details.dataset.target=row.target;
  if(selectedGroup)details.open=true;
  if(name!==row.target)fields.append(element('small',row.target));
  const listen=checkbox('旁听学习',row.listen),active=checkbox('主动参与',row.proactive);
  const save=button('保存群设置','');
  function update(){
   listen.input.disabled=row.excluded||!capabilities.listen;
   active.input.disabled=row.excluded||!capabilities.proactive||!listen.input.checked||(!row.reply_authorized&&!active.input.checked);
   save.disabled=row.excluded||(row.listen===listen.input.checked&&row.proactive===active.input.checked);
   details.toggleAttribute('data-dirty',!save.disabled);
  }
  listen.input.onchange=()=>{if(!listen.input.checked)active.input.checked=false;update();};
  active.input.onchange=update;
  save.onclick=()=>list.change('group_modes.save',{
   platform:list.args.platform,account:list.args.account,target:row.target,expected:row.version,
   listen:listen.input.checked,proactive:active.input.checked
  },'正在保存群设置…','群设置已保存。');
  fields.append(listen.label,active.label);
  if(row.excluded)fields.append(element('p','排除期间暂停旁听和发言。','note'));
  else if(capabilities.proactive)fields.append(element('p','开启主动参与后，角色会判断是否加入聊天，模型用量较高。','note'));
  if(capabilities.reason)fields.append(element('p',capabilities.reason,'note'));
  fields.append(save);
  update();return details;
 }

 const states={pending:'待提醒',processing:'处理中',sent:'已送达',completed:'已完成',cancelled:'已取消',uncertain:'送达未确认'};
 reminders.describeConflict=row=>'提醒状态已变化：'+(states[row.status]||'待核对')+'。';
 function localInput(date){
  const part=n=>String(n).padStart(2,'0');
  return date.getFullYear()+'-'+part(date.getMonth()+1)+'-'+part(date.getDate())+'T'+part(date.getHours())+':'+part(date.getMinutes());
 }
 function reminderRow(row,list){
  const when=localTime(row.due_at);
  const {details,fields}=detailRow(row.label||'提醒',[when||'时间未记录',states[row.status]||'状态待核对'].join(' · '));
  fields.append(element('p',row.recipient_label?'收件会话：'+row.recipient_label:'收件会话未记录','note'));
  const actions=element('div',null,'actions');
  if(row.can_reschedule===true){
   const label=element('label','新的提醒时间（本地）'),date=element('input');
   const initial=when?localInput(new Date(row.due_at)):'';
   date.type='datetime-local';date.required=true;date.value=initial;label.append(date);
   const save=button('保存改期');
   save.disabled=true;date.oninput=()=>{date.setCustomValidity('');save.disabled=!date.value||date.value===initial;};
   save.onclick=()=>{
    const value=new Date(date.value);
    if(!date.value||!Number.isFinite(value.getTime())||localInput(value)!==date.value||value.getTime()<=Date.now()){
     date.setCustomValidity('请选择有效的未来本地时间。');date.reportValidity();return;
    }
    void list.change('reminders.reschedule',{id:row.id,expected:row.version,due_at:value.toISOString()},'正在改期…','提醒已改期。');
   };
   fields.append(label);actions.append(save);
  }
  if(row.can_cancel===true){
   const cancel=button('取消提醒');
   cancel.onclick=()=>list.change('reminders.cancel',{id:row.id,expected:row.version},'正在取消提醒…','提醒已取消。');
   actions.append(cancel);
  }
  if(actions.childElementCount)fields.append(actions);
  return details;
 }

 function sync(){
  const data=document.body.dataset,visible=!document.hidden&&!data.roomOpen;
  const platform=visible&&data.navPage==='platforms'&&['qq','wechat'].includes(data.tabPlatforms)?data.tabPlatforms:null;
  if(platform!==lastPlatform)selectedGroup=null;lastPlatform=platform;
  const account=platform&&accounts[platform];
  const groupArgs=account?{platform,account,...(selectedGroup?{target:selectedGroup}:{})}:null;
  groupAccount.textContent=session&&account?platformNames[platform]+' · '+account:'';
  allGroups.hidden=!session||!selectedGroup;
  groups.scope(groupArgs,session,selectedGroup?'未找到这个群，请同步群目录后重试。':'尚无已登记的群，请同步群目录。',
   !platform?'':!session?'解锁桌面管理后可查看群设置。':!account?'请先选择平台账号。':'');
  const showingReminders=visible&&data.navPage==='records'&&data.tabRecords==='reminders';
  const pending=reminderFilter.value==='pending';
  reminders.scope(showingReminders?(pending?{status:'pending'}:{}):null,session,
   pending?'暂无待提醒事项。':'暂无提醒记录。',showingReminders&&!session?'解锁桌面管理后可查看提醒。':'');
  reminderFilter.disabled=!session;
 }
 allGroups.onclick=()=>{selectedGroup=null;sync();};
 reminderFilter.onchange=sync;
 window.QibanGroupModes={
  setAccount(platform,value){
   const account=value?String(value):null;
   if(accounts[platform]===account)return;
   accounts[platform]=account;if(lastPlatform===platform)selectedGroup=null;sync();
  },
  open(platform,target){
   if(!session||lastPlatform!==platform||!accounts[platform])return;
   selectedGroup=target;sync();groups.root.scrollIntoView({block:'start'});
   const heading=groups.root.querySelector('h2');heading.tabIndex=-1;heading.focus({preventScroll:true});
  },
  refresh(platform){if(lastPlatform===platform)groups.invalidate();}
 };
 window.addEventListener('qiban-workspace-access-change',({detail})=>{
  const next=detail.ready&&!detail.locked?detail.roomSession:null;
  if(session!==next){selectedGroup=null;reminderFilter.value='pending';if(session){accounts.qq=accounts.wechat=null;}}
  session=next;sync();
 });
 for(const event of ['qiban-nav-change','qiban-tab-change','qiban-room-view'])window.addEventListener(event,sync);
 document.addEventListener('visibilitychange',sync);
 // A background refresh must not replace a date or group preference being edited.
 setInterval(()=>{if(reminders.controller&&!reminders.items.querySelector('details[open]'))void reminders.read(reminders.history,'',true);},30000);
 sync();
})();
