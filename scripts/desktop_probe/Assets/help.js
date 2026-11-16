(()=>{
 'use strict';
 for(const tip of document.querySelectorAll('details.help')){
  const summary=tip.querySelector('summary'),body=tip.querySelector('.help-body');let pinned=false,hover=false,timer;
  function show(){clearTimeout(timer);tip.open=true;body.style.left='0px';const r=body.getBoundingClientRect();if(innerWidth>720&&r.right>innerWidth-16)body.style.left=`${innerWidth-16-r.right}px`;}
  function hide(){if(!pinned&&!hover&&!tip.contains(document.activeElement))tip.open=false;}
  tip.addEventListener('mouseenter',()=>{hover=true;show();});
  tip.addEventListener('mouseleave',()=>{hover=false;timer=setTimeout(hide,120);});
  tip.addEventListener('focusin',show);tip.addEventListener('focusout',()=>{pinned=false;queueMicrotask(hide);});
  summary.addEventListener('click',event=>{event.preventDefault();pinned=!pinned;if(pinned)show();else{tip.open=false;summary.blur();}});
  tip.addEventListener('keydown',event=>{if(event.key==='Escape'){pinned=false;tip.open=false;summary.blur();event.stopPropagation();}});
 }
})();
