/* Authenticated, session-scoped desktop lists. */
(() => {
 'use strict';
 const element=(tag,text,style)=>{
  const node=document.createElement(tag);
  if(text!=null)node.textContent=text;
  if(style)node.className=style;
  return node;
 };
 function button(text,style='secondary'){
  const node=element('button',text,style);node.type='button';return node;
 }
 function detailRow(title,subtitle){
  const details=element('details'),summary=element('summary');
  summary.append(element('strong',title),element('small',subtitle));details.append(summary);
  const fields=element('fieldset');details.append(fields);return {details,fields};
 }
 const clock=new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
 function localTime(value){
  const stamp=typeof value==='string'?Date.parse(value):NaN;
  return Number.isFinite(stamp)?clock.format(stamp):null;
 }
 class ManagementList {
  constructor(id,operation,row){
   this.root=document.getElementById(id);
   const find=name=>this.root.querySelector('[data-'+name+']');
   this.items=find('items');this.notice=find('notice');this.refresh=find('refresh');
   this.prev=find('prev');this.next=find('next');this.operation=operation;this.row=row;
   this.refresh.onclick=()=>this.read();
   this.prev.onclick=()=>this.read(this.history.slice(0,-1));
   this.next.onclick=()=>this.read([...this.history,this.nextCursor]);
  }
  scope(args,session,empty,unavailable){
   const key=JSON.stringify([args,session,unavailable]);
   if(this.scopeKey===key)return;
   this.controller?.abort();this.controller=null;this.scopeKey=key;
   this.args=args;this.empty=empty;this.history=[null];this.nextCursor=null;
   this.busy=false;this.refreshQueued=false;this.rows=null;this.items.replaceChildren();
   this.onPage?.(null);
   this.notice.textContent=unavailable||'';
   if(args&&session)this.controller=new AbortController();
   this.controls();if(this.controller)void this.read();
  }
  controls(){
   this.refresh.disabled=this.busy||!this.controller;
   this.prev.hidden=this.history.length<2;this.next.hidden=this.nextCursor==null;
   this.prev.disabled=this.next.disabled=this.busy;
   this.items.inert=this.busy;
   this.root.setAttribute('aria-busy',String(this.busy));
  }
  invalidate(){
   if(!this.controller)return;
   if(this.busy){this.refreshQueued=true;return;}
   if(!this.items.querySelector('[data-dirty]'))void this.read(this.history,'',true);
  }
  async read(history=this.history,message='',quiet=false){
   if(!this.controller||this.busy)return;
   const {signal}=this.controller;
   this.busy=true;this.controls();
   if(!quiet)this.notice.textContent='正在读取…';
   try{
    const page=await window.QibanWorkspace.call(this.operation,{...this.args,after:history.at(-1)},signal);
    if(signal.aborted)return;
    if(!Array.isArray(page?.items))throw Error('后台返回的列表无法读取。');
    this.onPage?.(page);
    this.history=history;this.nextCursor=page.next;
    if(!quiet||JSON.stringify(page.items)!==JSON.stringify(this.rows)){
     const expanded=new Set();
     if(quiet&&this.rows){
      Array.from(this.items.children).forEach((node,index)=>{
       if(node.open&&this.rows[index]?.id)expanded.add(this.rows[index].id);
      });
     }
     this.rows=page.items;
     this.items.replaceChildren(...page.items.map(item=>{
      const node=this.row(item,this);
      if(expanded.has(item.id))node.open=true;
      return node;
     }));
    }
    if(!quiet||!page.items.length||this.notice.textContent===this.empty)
     this.notice.textContent=[message,page.items.length?'':this.empty].filter(Boolean).join(' ');
   }catch(error){
    if(signal.aborted)return;
    this.notice.textContent=[message,error.message].filter(Boolean).join(' ');
   }finally{
    if(!signal.aborted){
     this.busy=false;this.controls();
     if(this.refreshQueued){this.refreshQueued=false;this.invalidate();}
    }
   }
  }
  async change(operation,args,progress,success){
   if(!this.controller||this.busy)return;
   const {signal}=this.controller;this.busy=true;this.controls();this.notice.textContent=progress;
   let message=success;
   try{await window.QibanWorkspace.call(operation,args,signal);}
   catch(error){
    if(signal.aborted)return;
    message=error.message;
    if(['edit_conflict','reminder_state'].includes(error.code)){
     message='状态已变化，请核对最新记录。';
     if(this.describeConflict){
      try{
       const page=await window.QibanWorkspace.call(this.operation,{id:args.id,after:null},signal);
       const current=page.items.find(item=>item.id===args.id);
       if(current)message=this.describeConflict(current);
      }catch(readError){if(!signal.aborted)message+=' '+readError.message;}
     }
    }
   }
   if(signal.aborted)return;
   this.busy=false;
   // A failed follow-up read must not leave an old permission or an editable stale version on screen.
   this.rows=null;this.items.replaceChildren();
   await this.read(this.keepPageAfterChange?this.history:[null],message);
  }
 }

 window.QibanDesktopLists={ManagementList,element,button,detailRow,localTime};
})();
