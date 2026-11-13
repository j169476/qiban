'use strict';
// NAV-171 · R6.1 应用内分区导航。
// 只做两件事：把当前分区写到 body 的 data-nav-page，并同步导航按钮的 aria-current。
// 不写 hidden/disabled，不移动或重建任何节点，不发送桥接消息，也不触发任何业务读取、
// 提交或取消；因此切换后表单内容、筛选和未保存输入保持原样，原 [hidden] 规则仍然优先。
(()=>{
 const pages=['home','persona','platforms','model','records','media','settings'];
 const body=document.body;
 const links=Array.from(document.querySelectorAll('[data-nav-target]'));
 function show(name){
  const page=pages.includes(name)?name:'home';
  body.dataset.navPage=page;
  for(const link of links){
   if(link.dataset.navTarget===page)link.setAttribute('aria-current','page');
   else link.removeAttribute('aria-current');
  }
 }
 for(const link of links)link.onclick=()=>{
  show(link.dataset.navTarget);
  if(typeof window.scrollTo==='function')window.scrollTo(0,0);
 };
 show(pages.includes(body.dataset.navPage)?body.dataset.navPage:'home');
})();
