(() => {
 'use strict';
 const el=id=>document.getElementById(id);
 const titles={home:'统一监控台',learn:'学习树',context:'上下文工作台',model:'模型与档位',persona:'人物与记忆',platforms:'平台与会话',records:'任务与记录',media:'素材与表情',settings:'设置与备份'};
 const names={local:'本机会客室',qq:'QQ',wechat:'微信',bilibili:'B 站'};
 const verbs={replying:'正在撰写回复',watching:'正在观看视频',learning:'正在学习',browsing:'正在浏览内容'};
 let previous=null,stale=false,paused=false;
 function menu(open){
  document.body.classList.toggle('house-menu-open',open);el('house-menu').setAttribute('aria-expanded',String(open));
 }
 function locationChanged(){
  const title=document.body.dataset.roomOpen?'会客室':titles[document.body.dataset.navPage]||titles.home;
  el('house-crumb').textContent=title;document.title=title+' · 栖伴';menu(false);
 }
 el('house-menu').onclick=()=>menu(!document.body.classList.contains('house-menu-open'));
 el('house-veil').onclick=()=>menu(false);
 el('house-monitor').onclick=()=>{document.querySelector('[data-nav-target="home"]').click();window.scrollTo({top:0,behavior:'instant'});};
 document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('house-menu-open')){menu(false);el('house-menu').focus();}});
 window.addEventListener('qiban-nav-change',locationChanged);
 window.addEventListener('qiban-room-view',locationChanged);
 const pageCount=()=>previous?.platforms.filter(p=>p.status==='active').length??0;
 function status(){
  el('house-monitor').textContent=stale?'监控 · 连接中断':paused?'监控 · 已暂停':previous?'监控 · '+pageCount()+' 运行':'监控 · 等待连接';
  el('house-monitor').dataset.live=String(!!previous&&!stale&&!paused);
 }
 function record(summary,stamp){
  const item=document.createElement('li'),time=document.createElement('time'),body=document.createElement('span');
  time.textContent=stamp?new Date(stamp).toLocaleTimeString('zh-CN',{hour12:false}):'';
  body.textContent=summary;item.append(time,body);const list=el('house-events');list.prepend(item);
  if(list.childElementCount>8)list.lastElementChild.remove();el('house-events-empty').hidden=true;
 }
 function snapshot(data){
  const when=Number.isFinite(Date.parse(data.generated_at))?data.generated_at:null;
  const initial=!previous||previous.runtime_instance!==data.runtime_instance;
  if(initial)record(previous?'已连接新的后台实例':'已收到运行快照',when);
  else if(stale)record('监控连接已恢复',when);
  else{
   for(const row of data.platforms){
    const before=previous.platforms.find(p=>p.platform===row.platform);
    const stateLabel=p=>p.failed?'运行中断':p.stopping?'正在停止':p.available===false?'未接入':
     p.status==='active'?(p.read_only?'只读运行':'运行中'):p.held||['paused','stopped'].includes(p.status)?'已停止':'状态已更新';
    if(before&&(before.status!==row.status||stateLabel(before)!==stateLabel(row)))
     record((names[row.platform]||row.platform)+' · '+stateLabel(row),when);
   }
   const activities=s=>(s.activities||[]).map(a=>(names[a.platform]||a.platform)+' · '+(verbs[a.state]||'正在处理'));
   const now=activities(data);if(now.join()!==activities(previous).join())record(now.join('、')||'当前活动已结束',when);
  }
  previous=data;stale=false;
  showPreset(data.active_preset);
  el('house-platform-count').textContent=String(data.platforms.length);
  status();
 }
 function showPreset(preset){
  el('house-active-preset').textContent=preset?.name||'';
  el('house-preset-line').hidden=!preset?.name;
 }
 window.addEventListener('qiban-context-applied',event=>showPreset(event.detail));
 function clear(){
  showPreset(null);
  previous=null;stale=false;paused=false;el('house-platform-count').textContent='';
  el('house-events').replaceChildren();el('house-events-empty').hidden=false;status();
 }
 window.QibanHouse={snapshot,clear,verb:activity=>verbs[activity]||'正在处理',pause(value){paused=value;status();},
  disconnected(){if(!stale)record('监控连接中断',Date.now());stale=true;status();}};
})();
