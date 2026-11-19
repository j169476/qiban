(() => {
 'use strict';
 const dialog=document.getElementById('public-reader'),title=document.getElementById('reader-title');
 const content=document.getElementById('reader-body'),choices=document.getElementById('reader-choices');
 const entries=[...document.querySelectorAll('.house-public-links a')];
 const parts=[...choices.querySelectorAll('button')];
 let page='',section='',opener=null,loading,returning=false;

 function loadPages(){
  if(window.qibanPublicPages)return Promise.resolve(window.qibanPublicPages);
  if(!loading)loading=new Promise((resolve,reject)=>{
   const script=document.createElement('script');
   script.src='/public-content.js';
   script.onload=()=>window.qibanPublicPages?resolve(window.qibanPublicPages):reject();
   script.onerror=()=>{script.remove();reject();};
   document.head.append(script);
  }).catch(error=>{loading=null;throw error;});
  return loading;
 }
 async function showPart(key){
  section=key;content.dataset.part=key;
  for(const button of parts)button.setAttribute('aria-pressed',String(button.dataset.readerPart===key));
  content.setAttribute('aria-busy','true');content.textContent='正在读取…';
  try{
   const pages=await loadPages();
   if(!dialog.open||section!==key)return;
   content.innerHTML=pages[key]; // Escaped article markup from the packaged build.
   content.scrollTop=0;
  }catch{
   if(!dialog.open||section!==key)return;
   content.textContent='本页未能读取。';
   const retry=document.createElement('button');retry.type='button';retry.textContent='重试';
   retry.onclick=()=>showPart(key);content.append(retry);
  }finally{if(section===key)content.removeAttribute('aria-busy');}
 }
 function route(){
  const next=location.hash.slice(1);
  returning=false;
  if(!['support','declaration'].includes(next)){
   if(dialog.open)dialog.close();
   if(page){
    const entry=opener||entries.find(a=>a.hash==='#'+page);
    const target=entry?.getClientRects().length?entry:document.getElementById('house-menu');
    page='';section='';target?.focus({preventScroll:true});opener=null;
   }
   return;
  }
  if(page===next&&dialog.open)return;
  page=next;const support=page==='support';
  title.textContent=support?'支持':'声明';choices.hidden=!support;
  if(!dialog.open)dialog.showModal();
  title.focus({preventScroll:true});showPart(support?'wechat':'declaration');
 }
 function back(){
  if(returning)return;
  returning=true;
  if(history.state?.qibanReader)history.back();
  else{history.replaceState(history.state,'',location.pathname+location.search);route();}
 }
 for(const entry of entries)entry.addEventListener('click',event=>{
  if(event.button!==0||event.ctrlKey||event.shiftKey||event.metaKey||event.altKey)return;
  event.preventDefault();opener=entry;
  if(location.hash!==entry.hash)history.pushState({qibanReader:true},'',entry.hash);
  route();
 });
 for(const button of parts)button.onclick=()=>showPart(button.dataset.readerPart);
 document.getElementById('reader-back').onclick=back;
 dialog.addEventListener('keydown',event=>{
  if(event.key==='Escape'){event.preventDefault();event.stopPropagation();back();}
 });
 dialog.addEventListener('cancel',event=>{event.preventDefault();back();});
 window.addEventListener('popstate',route);window.addEventListener('hashchange',route);
 route();
})();
