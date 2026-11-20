(() => {
 'use strict';
 const {escape:e,option,button,label}=window.QibanWorkspace;
 let serial=0;
 // A native button makes each short explanation available with a mouse, touch or keyboard.
 function help(title,description,id){
  return '<span class="ws-help"><button type="button" class="ws-help-icon" aria-label="'+e(title+'的说明')+'" aria-describedby="'+id+'">!</button>'+
   '<span class="ws-tip" role="tooltip" id="'+id+'">'+e(description)+'</span></span>';
 }
 function field(title,description,control){
  const id='context-input-'+(++serial);
  return '<div class="ws-field"><div class="ws-caption"><label for="'+id+'">'+e(title)+'</label>'+help(title,description,id+'-tip')+
   '</div>'+control.replace(/^<(input|select|textarea)/,'<$1 id="'+id+'" aria-describedby="'+id+'-tip"')+'</div>';
 }
 function catalog(request){
  const items=new Map();
  let after=null;
  function put(item){if(item?.id)items.set(item.id,{id:item.id,name:item.name,revision:item.revision});}
  async function list(more=false){
   const page=await request('context.presets',{after:more?after:null});
   if(!more)items.clear();
   page.items.forEach(put);after=page.after;
  }
  function choices(selected){
   return [...items.values()].map(p=>option(p.id,p.name,selected===p.id)).join('')+
    (after?option('@more','更多方案…'):'');
  }
  return {put,list,choices,get:id=>items.get(id)};
 }

 function replies({catalog,selected,signal}){
  const p=window.QibanSources.panel('回复记录',signal);
  if(!p)return;
  const filter='按方案查看';
  p.body.innerHTML=field(filter,'查看使用这个方案时发出的回复。','<select data-reply-preset>'+catalog.choices(selected)+'</select>')+
   '<div class="ws-replies"></div><div class="ws-actions">'+button('reply-refresh','刷新')+button('reply-more','更多记录')+'</div>';
  const pick=p.body.querySelector('select'),rows=p.body.querySelector('.ws-replies'),more=p.body.querySelector('[data-ws=reply-more]');
  let pageCount=1,sequence=0,inFlight=false,active=selected;
  function row(item){
   const date=item.at==null?null:new Date(typeof item.at==='number'?item.at*1000:item.at);
   const stamp=date&&!Number.isNaN(date.getTime())?date.toLocaleString():'';
   return '<article><small>'+e([stamp,label(item.platform)].filter(Boolean).join(' · '))+'</small><p>'+e(item.text)+'</p></article>';
  }
  async function refresh(showStatus=false){
   const seq=++sequence,id=active;
   inFlight=true;more.disabled=true;
   if(showStatus)p.status('正在读取回复…');
   try{
    let after=null,items=[],pages=0;
    do{
     const page=await p.call('context.preset_replies',{id,after});
     if(p.signal.aborted||seq!==sequence)return;
     items.push(...page.items);after=page.after;pages++;
    }while(after&&pages<pageCount);
    pageCount=pages;
    const content=items.length?items.map(row).join(''):'<p>这个方案还没有回复记录。</p>';
    if(rows.dataset.plan!==id||rows._content!==content){rows.innerHTML=content;rows._content=content;rows.dataset.plan=id;}
    more.hidden=!after;p.status('');
   }catch(error){
    if(!p.signal.aborted&&seq===sequence){rows.replaceChildren();rows._content=null;more.hidden=true;p.status(error.message);}
   }finally{if(seq===sequence){inFlight=false;more.disabled=false;}}
  }
  pick.onchange=async()=>{
   if(pick.value==='@more'){
    pick.value=active;pick.disabled=true;
    try{await catalog.list(true);if(!p.signal.aborted)pick.innerHTML=catalog.choices(active);}
    catch(error){if(!p.signal.aborted)p.status(error.message);}
    finally{pick.disabled=false;}
    return;
   }
   active=pick.value;pageCount=1;rows.replaceChildren();rows._content=null;more.hidden=true;void refresh(true);
  };
  more.hidden=true;
  p.body.onclick=ev=>{
   const action=ev.target.closest('[data-ws]')?.dataset.ws;
   if(action==='reply-more'&&!inFlight){pageCount++;void refresh(true);}
   if(action==='reply-refresh')void refresh(true);
  };
  const revisit=()=>{if(!document.hidden&&!inFlight)void refresh();};
  const clock=setInterval(revisit,12000);
  document.addEventListener('visibilitychange',revisit,{signal:p.signal});
  window.addEventListener('focus',revisit,{signal:p.signal});
  p.signal.addEventListener('abort',()=>clearInterval(clock),{once:true});
  void refresh(true);
 }
 window.QibanPresets={catalog,field,help,replies};
})();
