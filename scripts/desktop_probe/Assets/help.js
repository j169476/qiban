(()=>{
 'use strict';
 // Measure after opening so wrapped text, desktop zoom and modal edges are respected.
 function place(box,anchor){
  box.style.translate='';
  if(getComputedStyle(box).position==='fixed')return;
  const r=box.getBoundingClientRect();if(!r.width)return;
  const bounds=anchor.closest('dialog[open]')?.getBoundingClientRect();
  const left=Math.max(8,(bounds?.left??0)+8),right=Math.min(innerWidth-8,(bounds?.right??innerWidth)-8);
  const floor=Math.min(innerHeight,bounds?.bottom??innerHeight)-8;
  const dx=Math.max(left-r.left,Math.min(0,right-r.right));
  const above=anchor.getBoundingClientRect().top-r.height-6;
  const dy=r.bottom>floor&&above>=Math.max(4,bounds?.top??0)?above-r.top:0;
  const scale=r.width/box.offsetWidth;
  box.style.translate=(dx/scale)+'px '+(dy/scale)+'px';
 }
 for(const tip of document.querySelectorAll('details.help')){
  const summary=tip.querySelector('summary'),body=tip.querySelector('.help-body');let pinned=false,hover=false,timer;
  function show(){clearTimeout(timer);tip.open=true;place(body,summary);}
  function hide(){if(!pinned&&!hover&&!tip.contains(document.activeElement))tip.open=false;}
  tip.addEventListener('mouseenter',()=>{hover=true;show();});
  tip.addEventListener('mouseleave',()=>{hover=false;timer=setTimeout(hide,120);});
  tip.addEventListener('focusin',show);tip.addEventListener('focusout',()=>{pinned=false;queueMicrotask(hide);});
  summary.addEventListener('click',event=>{event.preventDefault();pinned=!pinned;if(pinned)show();else tip.open=false;});
  tip.addEventListener('keydown',event=>{if(event.key==='Escape'){pinned=false;tip.open=false;event.preventDefault();event.stopPropagation();}});
 }
 // Workspace buttons are created on demand, including inside dialogs.
 for(const type of ['focusin','pointerover','click'])document.addEventListener(type,event=>{
  const hint=event.target.closest?.('.ws-help');if(!hint)return;
  hint.removeAttribute('data-dismissed');place(hint.querySelector('.ws-tip'),hint);
 });
 document.addEventListener('keydown',event=>{
  const hint=event.target.closest?.('.ws-help');
  if(event.key==='Escape'&&hint&&!hint.hasAttribute('data-dismissed')){
   hint.dataset.dismissed='';event.preventDefault();event.stopPropagation();
  }
 },true);
 window.addEventListener('resize',()=>{
  for(const tip of document.querySelectorAll('details.help[open]'))place(tip.querySelector('.help-body'),tip.querySelector('summary'));
  for(const hint of document.querySelectorAll('.ws-help:is(:hover,:focus-within)'))place(hint.querySelector('.ws-tip'),hint);
 });
})();
