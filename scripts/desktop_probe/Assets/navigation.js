'use strict';
// LAYOUT-172 · 应用内分区导航、分区内的分组标签，以及顶栏“应用设置”菜单的开合。
// 显示层只做三件事：把当前分区/标签写到 body 的 data-* 上、同步按钮的 aria 状态、
// 开合顶栏菜单并把需要跳转的菜单项滚到对应区域。
// 不写 hidden/disabled，不移动或重建任何节点，不克隆重排，不发送桥接消息，也不触发任何
// 业务读取、提交或取消；因此切换后表单内容、筛选和未保存输入保持原样，原 [hidden] 规则
// 仍然优先。菜单与标签只改 aria-expanded / aria-pressed，显隐由 CSS 决定。
(()=>{
 const body=document.body;
 const links=Array.from(document.querySelectorAll('[data-nav-target]'));
 const pages=links.map(link=>link.dataset.navTarget);
 const tabs=Array.from(document.querySelectorAll('[data-tab-target]'));
 const byId=id=>typeof document.getElementById==='function'?document.getElementById(id):null;
 const toggle=byId('app-settings'), menu=byId('app-settings-menu');
 const canListen=typeof document.addEventListener==='function';
 const expanded=()=>!toggle||toggle.getAttribute('aria-expanded')==='true';
 // 分区切换后广播一次，供会客室按原 close/reset 关闭（见 room.js）。不发送任何桥接消息。
 function broadcast(type,detail){
  if(typeof CustomEvent!=='function'||typeof window.dispatchEvent!=='function')return;
  window.dispatchEvent(new CustomEvent(type,{detail}));
 }
 function show(name){
  const page=pages.includes(name)?name:'home';
  body.dataset.navPage=page;
  for(const link of links){
   if(link.dataset.navTarget===page)link.setAttribute('aria-current','page');
   else link.removeAttribute('aria-current');
  }
  broadcast('qiban-nav-change',{page});
 }
 // 分组标签：把选中项写到 body[data-tab-<group>]，只同步 aria-pressed，显隐全部交给 CSS。
 function selectTab(group,target){
  if(typeof group!=='string'||!group||typeof target!=='string'||!target)return;
  body.dataset['tab'+group[0].toUpperCase()+group.slice(1)]=target;
  for(const tab of tabs){
   if(tab.dataset.tabGroup!==group)continue;
   tab.setAttribute('aria-pressed',String(tab.dataset.tabTarget===target));
  }
  broadcast('qiban-tab-change',{group,target});
 }
 function closeMenu(restore){
  if(!toggle)return;
  const open=expanded();
  toggle.setAttribute('aria-expanded','false');
  if(open&&restore&&typeof toggle.focus==='function')toggle.focus();
 }
 function openMenu(){
  if(toggle)toggle.setAttribute('aria-expanded','true');
 }
 // 菜单项里的跳转：先切到对应分区，再滚到目标区并聚焦其中第一个可用控件。
 // 只读 disabled 判断能否聚焦，不修改控件状态。
 function jump(identifier){
  closeMenu(false);
  show('settings');
  const node=byId(identifier);
  if(!node)return;
  if(typeof node.scrollIntoView==='function')node.scrollIntoView({block:'start'});
  const first=typeof node.querySelector==='function'?node.querySelector('input,select,textarea,button,summary'):null;
  if(first&&!first.disabled&&typeof first.focus==='function')first.focus();
 }
 for(const link of links)link.onclick=()=>{
  show(link.dataset.navTarget);
  if(typeof window.scrollTo==='function')window.scrollTo({top:0,behavior:'instant'});
 };
 for(const tab of tabs){
  if(typeof tab.dataset.tabGroup!=='string'||typeof tab.dataset.tabTarget!=='string')continue;
  tab.onclick=()=>selectTab(tab.dataset.tabGroup,tab.dataset.tabTarget);
 }
 const jumpers=Array.from(document.querySelectorAll('[data-jump]'));
 for(const item of jumpers){
  if(typeof item.dataset.jump!=='string'||!item.dataset.jump)continue;
  item.onclick=()=>jump(item.dataset.jump);
 }
 if(toggle)toggle.onclick=()=>{
  if(expanded())closeMenu(false);else openMenu();
 };
 if(canListen)document.addEventListener('keydown',event=>{
  if(event.key!=='Escape'&&event.key!=='Esc')return;
  if(!expanded())return;
  // 菜单开着时 Esc 只收菜单，不再继续关闭别的层（例如会客室）。
  if(typeof event.stopPropagation==='function')event.stopPropagation();
  closeMenu(true);
 },true);
 if(canListen)document.addEventListener('click',event=>{
  const target=event.target;
  if(!expanded())return;
  const inside=node=>!!node&&typeof node.contains==='function'&&node.contains(target);
  if(inside(menu)||inside(toggle))return;
  closeMenu(false);
 });
 if(canListen)document.addEventListener('click',event=>{
  const target=event.target;
  if(!target||typeof target.closest!=='function'||!target.closest('.probe-menu-item'))return;
  closeMenu(false);
 });
 // 首次进入：以 body 上已有的分区/标签为准（HTML 里已写好默认值），只同步按钮的 aria 状态。
 show(pages.includes(body.dataset.navPage)?body.dataset.navPage:'home');
 for(const tab of tabs){
  const group=tab.dataset.tabGroup;
  if(typeof group!=='string'||!group)continue;
  const current=body.dataset['tab'+group[0].toUpperCase()+group.slice(1)];
  if(current)tab.setAttribute('aria-pressed',String(tab.dataset.tabTarget===current));
 }
})();
