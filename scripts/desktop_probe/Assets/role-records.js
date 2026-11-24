/* Administrator views for ongoing matters and the results of background privacy reviews. */
(() => {
 'use strict';
 const {ManagementList,element,button,detailRow,localTime}=window.QibanDesktopLists;
 const phases={
  ready:'待续办',processing:'正在处理',
  stopped:'已停止',completed:'已结束',uncertain:'待核对'
 };
 const receipts={none:'',sent:'投递已确认',uncertain:'投递结果不明',cancelled:'本次投递已取消'};
 const actionNames={chat:'会话消息',alarm:'定时提醒',wait:'等待',end:'结束'};
 const levels={credential:'凭据',private:'个人私密',circle:'圈内共知',social:'普通社交可用'};
 const reviews={pending:'等待后台审查',reviewed:'已审查',corrected:'已纠正'};
 const categories={
  credential:'凭据',name:'姓名或称呼',home:'住址',venue:'场馆地点',
  itinerary:'个人行程',meeting:'活动安排',ordinary:'一般信息'
 };
 const matters=new ManagementList('matters-panel','matters.list',matterRow);
 const privacy=new ManagementList('privacy-panel','privacy.list',privacyRow);
 matters.keepPageAfterChange=privacy.keepPageAfterChange=true;
 matters.describeConflict=row=>'事项已变化：'+matterStatus(row)+'。';
 privacy.describeConflict=row=>'范围已变化：'+(levels[row.level]||reviews[row.status]||'待核对')+'。';
 const disclosure=privacy.root.querySelector('[data-disclosure]');
 const scope=privacy.root.querySelector('[data-scope]');
 const allMatters=matters.root.querySelector('[data-all]');
 let source=null,session=null,selectedMatter=null;

 function matterStatus(row){
  let phase=phases[row.status]||'状态待核对';
  if(row.status==='waiting'){
   const wake=localTime(row.wake_at);
   phase=wake?'等待唤醒 · '+wake:row.wake_at?'唤醒时间待核对':'等待新消息';
  }
  return [phase,receipts[row.delivery]].filter(Boolean).join(' · ');
 }
 function actionContent(row,link=false){
  const content=element('div');
  const title=[row.label||'自主事项',actionNames[row.action]].filter(Boolean).join(' · ');
  const heading=element('strong',title);
  if(link&&row.id){
   const open=button(title,'action-link');open.onclick=()=>openMatters(row.id);heading.replaceChildren(open);
  }
  content.append(heading,element('small',matterStatus(row)));
  if(row.recipient_label)content.append(element('small','收件会话：'+row.recipient_label));
  return content;
 }
 function matterRow(row,list){
  const item=element('article',null,'matter-row');
  item.append(actionContent(row));
  if(row.can_stop===true){
   const stop=button('停止续办');
   stop.setAttribute('aria-label','停止续办：'+(row.label||'自主事项'));
   stop.onclick=()=>list.change('matters.stop',{id:row.id,expected:row.version},'正在停止续办…','已停止后续处理。');
   item.append(stop);
  }
  return item;
 }

 const recent=document.getElementById('house-actions');
 const recentList=recent.querySelector('ol');
 let lastRows=null;
 window.QibanRoleRecords={
  showRecent(rows){
   const data=Array.isArray(rows)?rows:null;
   recent.hidden=data===null;
   if(data===null){lastRows=null;recentList.replaceChildren();return;}
   const signature=JSON.stringify(data.map(r=>[r.id,r.label,r.action,r.status,r.recipient_label,r.wake_at,r.delivery,r.updated_at]));
   if(lastRows===signature)return;
   lastRows=signature;
   recentList.replaceChildren(...data.map(row=>{
    const item=element('li');item.append(actionContent(row,true));
    const time=localTime(row.updated_at);
    if(time){const stamp=element('time',time);stamp.dateTime=row.updated_at;item.append(stamp);}
    return item;
   }));
   recent.querySelector('[data-empty]').hidden=data.length>0;
  }
 };
 function openMatters(id){
  selectedMatter=id;
  document.querySelector('[data-nav-target="records"]').click();
  document.querySelector('[data-tab-group="records"][data-tab-target="matters"]').click();
  const heading=matters.root.querySelector('h2');heading.tabIndex=-1;heading.focus();
 }
 recent.querySelector('.house-card-head button').onclick=()=>openMatters(null);
 allMatters.onclick=()=>{selectedMatter=null;sync();};

 function privacyRow(row,list){
  // Only the administrative projection belongs here. Never render source text or model output.
  const secret=row.level==='credential'||row.category==='credential';
  const title=secret?'凭据（已遮盖）':row.label||'隐私片段';
  const state=reviews[row.status]||'状态待核对';
  const summary=secret?state:[categories[row.category],levels[row.level],state]
   .filter((value,index,parts)=>value&&parts.indexOf(value)===index).join(' · ');
  const {details,fields}=detailRow(title,summary);
  fields.append(element('p','当前受众：'+(row.audience||'尚未确定'),'note'));
  const choices=Array.isArray(row.choices)?row.choices.filter(level=>Object.hasOwn(levels,level)):[];
  if(secret||!choices.some(level=>level!==row.level))return details;
  const label=element('label','更正使用范围');
  const select=element('select');
  const current=element('option',levels[row.level]||'选择范围');
  current.value='';select.append(current);
  for(const level of new Set(choices)){
   if(level===row.level)continue;
   const option=element('option',levels[level]);option.value=level;select.append(option);
  }
  label.append(select);
  const save=button('保存范围');
  save.disabled=true;
  select.onchange=()=>{
   save.disabled=!select.value;
   details.toggleAttribute('data-dirty',!save.disabled);
  };
  save.onclick=()=>list.change('privacy.correct',{id:row.id,expected:row.version,level:select.value},
   '正在保存范围…','使用范围已更新。');
  fields.append(label,save);return details;
 }

 function sharingPage(){
  const data=document.body.dataset;
  return data.navPage==='persona'&&data.tabPersona==='sharing'&&!data.roomOpen;
 }
 function sync(){
  const visible=!document.hidden&&!document.body.dataset.roomOpen;
  const data=document.body.dataset;
  const onMatters=data.navPage==='records'&&data.tabRecords==='matters';
  if(!onMatters&&matters.args)selectedMatter=null;
  const showMatters=visible&&onMatters;
  allMatters.hidden=!selectedMatter;
  matters.scope(showMatters?(selectedMatter?{id:selectedMatter}:{}):null,session,selectedMatter?'找不到这条事项。':'暂无自主事项。',showMatters&&!session?'解锁桌面管理后可查看自主事项。':'');
  if(!sharingPage()){source=null;disclosure.open=false;}
  scope.hidden=!source;
  const showPrivacy=visible&&sharingPage()&&disclosure.open;
  privacy.scope(showPrivacy?(source||{}):null,session,source?'这条资料暂无隐私片段。':'暂无隐私审查记录。',
   showPrivacy&&!session?'解锁桌面管理后可查看隐私范围。':'');
 }
 window.QibanPrivacy={
  selectSource(kind,identifier){
   if(source&&(source.kind!==kind||source.identifier!==identifier)){
    source=null;disclosure.open=false;sync();
   }
  },
  open(kind,identifier){
   if(!session||!sharingPage())return;
   source={kind,identifier};disclosure.open=true;sync();
   privacy.root.scrollIntoView({block:'start'});disclosure.querySelector('summary').focus({preventScroll:true});
  }
 };
 privacy.root.querySelector('[data-all]').onclick=()=>{source=null;sync();};
 disclosure.addEventListener('toggle',sync);
 window.addEventListener('qiban-workspace-access-change',({detail})=>{
  const next=detail.ready&&!detail.locked?detail.roomSession:null;
  if(next!==session){source=null;selectedMatter=null;disclosure.open=false;window.QibanRoleRecords.showRecent(null);}
  session=next;sync();
 });
 for(const event of ['qiban-nav-change','qiban-tab-change','qiban-room-view'])window.addEventListener(event,sync);
 document.addEventListener('visibilitychange',sync);
 setInterval(()=>{
  if(matters.controller)matters.invalidate();
  if(privacy.controller&&!privacy.items.querySelector('select:focus,button:focus'))privacy.invalidate();
 },15000);
 sync();
})();
