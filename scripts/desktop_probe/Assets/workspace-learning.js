(() => {
 'use strict';
 const ui=window.QibanWorkspace, {escape:esc,option,button}=ui;
 ui.register('learn',async({body,signal,request,has,notice,showError,reset})=>{
  let account,goals=[],after=null,scope,epoch=0,snapshot,map,poll,attempt=0;
  let querying=false,readingGoals=false,paused=false,blocked=false,writing=false,detailSeq=0,searchSeq=0;
  let results=[],nextOffset=null,node=null,recovering=null,reselect=null,lastQuery='',lastFilter='all';
  const known=new Map();
  body.innerHTML='<div class="ws-actions"><label>学习目标<select data-goals></select></label><span data-goal-more></span>'+button('refresh','重新读取')+
   (has('learning.index_status')?button('index-status','索引状态'):'')+'</div><div class="ws-index ws-meta" role="status"></div><div class="ws-content"></div>';
  const el=s=>body.querySelector(s);
  const current=id=>id===epoch&&!signal.aborted;
  const indexStatus=el('.ws-index'),content=el('.ws-content');
  const clearPoll=()=>clearTimeout(poll);
  function drop(){
   ++epoch;++detailSeq;++searchSeq;clearPoll();map?.close();map=null;snapshot=null;node=null;querying=false;writing=false;blocked=false;paused=false;
   known.clear();results=[];nextOffset=null;content.replaceChildren();indexStatus.replaceChildren();
  }
  signal.addEventListener('abort',drop,{once:true});
  function paintGoals(){
   el('[data-goals]').innerHTML=goals.map(g=>option(g.id,g.title,g.id===scope?.goal)).join('');
   el('[data-goal-more]').innerHTML=after?button('more-goals','更多目标',readingGoals):'';
  }
  async function directory(more=false){
   if(readingGoals)return;
   readingGoals=true;const id=epoch;
   try{
    const page=await request('learning.goals',{after:more?after:''});if(!current(id))return;
    if(more&&account!==page.account){reset();return;}
    account=page.account;goals=more?[...goals,...page.items]:page.items;after=page.after;
    if(!more){choose(goals[0]);if(!goals.length)notice('当前账号还没有可查看的学习目标。');}
    paintGoals();
   }catch(error){showError(error);}
   finally{readingGoals=false;if(!signal.aborted)paintGoals();}
  }
  function choose(goal,keep=null){
   drop();scope=goal?{account,goal:goal.id,definition:goal.definition}:null;attempt=0;
   recovering=keep;reselect=keep?.id||null;
   if(!scope)return;
   if(!has('learning.snapshot')){notice('当前后台仅提供目标目录。');return;}
   void getSnapshot();
  }
  function schedule(){
   clearPoll();if(!paused&&!document.hidden&&!signal.aborted&&scope&&!snapshot)
    poll=setTimeout(getSnapshot,Math.min(4000,600*2**Math.min(3,attempt++)));
  }
  function invalidate(keep=null){
   const g=goals.find(g=>g.id===scope?.goal);
   choose(g,keep);
  }
  function handle(error,id){
   if(!current(id))return;
   if(['account_changed','learning_unavailable','bili_login_required','bili_unavailable'].includes(error.code)){
    drop();scope=null;recovering=null;reselect=null;goals=[];after=null;paintGoals();showError(error);
    if(error.code==='account_changed'||error.code==='learning_unavailable')void directory();
   }else if(error.code==='edit_conflict'||error.code==='index_pending'){
    const keep=node&&formValues()?{id:node.id,changes:formValues(),isNew:node.version===null}:null;
    invalidate(keep);
   }else{
    if(['locked','restore_pending'].includes(error.code)){blocked=true;const edit=el('[data-editor] fieldset');if(edit)edit.disabled=true;}
    showError(error);
   }
  }
  async function getSnapshot(){
   clearPoll();if(querying||!scope||signal.aborted||document.hidden)return;
   const id=epoch;querying=true;
   try{
    const data=await request('learning.snapshot',scope);if(!current(id))return;
    const idx=data.index||{};paused=!!idx.paused;
    indexStatus.textContent=paused?'索引已暂停':data.ready?data.count+' 个节点':'正在整理学习树…';
    if(idx.error)indexStatus.append(document.createTextNode(' · '+idx.error));
    if(has('learning.index_control'))indexStatus.insertAdjacentHTML('beforeend',button('index',paused?'继续整理':'暂停整理'));
    if(!data.ready){notice(paused?'恢复整理后继续读取。':idx.error?'索引暂不可用，可重新读取。':'');if(!idx.error)schedule();return;}
    snapshot=data;notice('');mount(id);
    if(reselect){const key=reselect;reselect=null;void detail(key,id);}
   }catch(error){
    if(current(id)&&error.code==='edit_conflict'){
     // A new definition needs a fresh directory entry, not the old scope again.
     drop();scope=null;goals=[];after=null;recovering=null;paintGoals();notice('目标定义已变化，正在重新读取目录。');void directory();
    }else if(current(id)&&error.code==='index_pending')schedule();else handle(error,id);
   }finally{if(current(id))querying=false;}
  }
  async function scoped(op,args={}){
   return request(op,{...scope,snapshot:snapshot.snapshot,...args});
  }
  function mount(id){
   const searchable=has('learning.search');
   content.innerHTML=(has('learning.tile')?'<div data-map></div>':'')+
    '<div class="ws-columns"><section><div class="ws-actions">'+(has('learning.edit')?button('new','添加笔记'):'')+'</div>'+
    (searchable?'<form data-search><label>查找学习内容<input name="query" maxlength="256" placeholder="标题或正文"></label><label>范围<select name="filter">'+[['all','全部'],['active','活跃'],['archived','已归档'],['due','待复习']].map(([v,t])=>option(v,t)).join('')+'</select></label><button>查找</button></form><div data-results></div>':'')+'</section><section data-editor aria-label="学习内容详情"></section></div>';
   if(has('learning.tile'))map=ui.learningMap(el('[data-map]'),snapshot,{
    load:args=>scoped('learning.tile',args),select:has('learning.detail')?key=>{if(!writing)void detail(key,id);}:null,
    onError:error=>handle(error,id),signal
   });
   if(searchable){el('[data-search]').onsubmit=e=>{e.preventDefault();void search(false);};void search(false);}
  }
  async function search(more){
   const id=epoch,seq=++searchSeq,form=el('[data-search]');
   if(!more){lastQuery=form.elements.query.value;lastFilter=form.elements.filter.value;}
   const query=lastQuery,filter=lastFilter;
   if(!more){nextOffset=null;results=[];el('[data-results]').textContent='正在查找…';}
   const next=el('[data-ws="more"]');if(next)next.disabled=true;
   try{
    const page=await scoped('learning.search',{query,filter,offset:more?nextOffset:0});
    if(!current(id)||seq!==searchSeq)return;
    nextOffset=page.next_offset;results=more?[...results,...page.items]:page.items;
    for(const n of page.items)known.set(n.id,n);
    el('[data-results]').innerHTML=results.map(n=>has('learning.detail')?'<button class="secondary ws-row" data-item="'+esc(n.id)+'">'+esc(n.title)+(n.archived?' · 已归档':'')+'</button>':'<p>'+esc(n.title)+'</p>').join('')||'<p>没有符合条件的内容。</p>';
    if(nextOffset!=null)el('[data-results]').insertAdjacentHTML('beforeend',button('more','更多结果'));
    if(node)relationOptions();
   }catch(error){if(seq===searchSeq)handle(error,id);}
  }
  async function detail(key,id=epoch){
   if(!has('learning.detail')||writing)return;
   const seq=++detailSeq;node=null;el('[data-editor]').textContent='正在读取详情…';
   try{
    const n=await scoped('learning.detail',{node:key});if(!current(id)||seq!==detailSeq)return;
    known.set(n.id,n);node=n;map?.focus(n);paintNode();
    if(recovering?.id===key)notice('已取回当前版本，请核对内容后决定是否带回上次输入。');
   }catch(error){
    if(!current(id)||seq!==detailSeq)return;
    if(error.code==='learning_unavailable'&&recovering?.id===key&&recovering.isNew){
     newNote(key);applyRecovery();notice('当前快照尚无这条笔记，可用原编号再次保存。');
    }else handle(error,id);
   }
  }
  function newNote(key){
   ++detailSeq;node={id:key||'note-'+crypto.randomUUID().replaceAll('-',''),version:null,type:'note',title:'',body:'',parent_id:null,prerequisites:[],archived:false};
   paintNode();el('[name=title]')?.focus();
  }
  function relationOptions(){
   const editor=el('[data-editor]');if(!editor||!node)return;
   for(const name of ['parent','pre']){
    const field=editor.querySelector('[name='+name+']');if(!field)continue;
    const selected=field.options.length?Array.from(field.selectedOptions,o=>o.value).filter(Boolean):name==='pre'?node.prerequisites:[node.parent_id].filter(Boolean);
    const choices=new Set([...selected,...results.filter(n=>node.type!=='criterion'||n.type==='criterion').map(n=>n.id)]);
    choices.delete(node.id);
    field.innerHTML=(name==='parent'?option('','无',!selected.length):'')+[...choices].map(k=>option(k,known.get(k)?.title||k,selected.includes(k))).join('');
   }
  }
  function paintNode(){
   const n=node,isNote=n.type==='note',can=has('learning.edit')&&!blocked;
   const notes='<label>标题<input required name="title" maxlength="500" value="'+esc(n.title)+'"></label><label>正文<textarea name="body" maxlength="8000" rows="5">'+esc(n.body)+'</textarea></label><label><input name="archived" type="checkbox" '+(n.archived?'checked':'')+'> 归档笔记</label>';
   const grade=n.mastery==null?'暂无有效评分':Math.round(n.mastery*100)+'%';
   const evidence=n.assessment?' · 测验 '+esc(n.assessment.id):'';
   const mistakes=(n.mistakes||[]).map(m=>'<li><strong>'+esc(m.question)+'</strong><p>'+esc(m.gap)+'</p><small>'+esc(m.resolved?'已订正':'待订正')+'</small></li>').join('');
   el('[data-editor]').innerHTML='<h3>'+esc(n.version===null?'新笔记':n.title)+'</h3>'+
    (!isNote?'<p>掌握度 '+grade+evidence+'</p><p>'+esc(n.due?'已到复习时间':n.next_review_at?'下次复习 '+n.next_review_at:'暂无复习日期')+'</p>':'')+
    (n.source?'<details><summary>内容来源</summary><p>'+esc(goals.find(g=>g.id===n.source.id)?.title||n.source.id)+' · '+esc(n.source.version)+'</p></details>':'')+
    (mistakes?'<details><summary>错题记录</summary><ul>'+mistakes+'</ul></details>':'')+
    '<form data-node-form><fieldset '+(!can?'disabled':'')+'>'+(isNote?notes:'')+
    '<label>父节点<select name="parent"></select></label><label>前置项<select name="pre" multiple size="4"></select></label><small>查找左侧内容可增加关系候选项。</small>'+
    (can?'<div class="ws-actions"><button>保存节点</button></div>':'')+'</fieldset></form>'+
    (recovering?.id===n.id&&can?button('recover','带回上次输入'):'');
   relationOptions();el('[data-node-form]').onsubmit=e=>{e.preventDefault();void save();};
  }
  function formValues(){
   const f=el('[data-node-form]')?.elements;if(!f||!node)return null;
   const v={parent_id:f.parent.value||null,prerequisites:Array.from(f.pre.selectedOptions,o=>o.value)};
   if(node.type==='note')Object.assign(v,{title:f.title.value,body:f.body.value,archived:f.archived.checked});
   return v;
  }
  function applyRecovery(){
   if(!node||node.id!==recovering?.id)return;
   node={...node,...recovering.changes};recovering=null;paintNode();
  }
  async function save(){
   if(writing||blocked||!node||!has('learning.edit'))return;
   const id=epoch,keep={id:node.id,changes:formValues(),isNew:node.version===null};
   const expected=node.version;writing=true;el('[data-node-form] fieldset').disabled=true;notice('正在保存…');
   try{
    const ack=await scoped('learning.edit',{node:keep.id,expected,changes:keep.changes});
    if(!current(id))return;
    invalidate();reselect=ack.node;notice('已保存，正在更新学习树。');
   }catch(error){
    if(!current(id))return;
    if(['edit_conflict','index_pending','timeout'].includes(error.code)){invalidate(keep);notice('正在核对原节点的最新版本。');}
    else handle(error,id);
   }finally{if(current(id)){writing=false;const f=el('[data-node-form] fieldset');if(f)f.disabled=blocked;}}
  }
  el('[data-goals]').onchange=event=>choose(goals.find(g=>g.id===event.target.value));
  body.onclick=async event=>{
   const b=event.target.closest('button');if(!b||b.disabled||writing)return;
   const a=b.dataset.ws;
   if(b.dataset.item){recovering=null;void detail(b.dataset.item);}
   else if(a==='new'){recovering=null;newNote();}
   else if(a==='more')void search(true);
   else if(a==='recover')applyRecovery();
   else if(a==='more-goals')void directory(true);
   else if(a==='refresh'){
    if(!scope)void directory();
    else if(!snapshot)void getSnapshot();
    else invalidate();
   }
   else if(a==='index-status'){
    const id=epoch;b.disabled=true;
    try{const state=await request('learning.index_status');if(current(id))notice((state.paused?'已暂停':'可整理')+' · 等待 '+(state.pending??0)+' · 最近完成 '+(state.last_success?new Date(state.last_success).toLocaleString():'暂无')+(state.error?' · '+state.error:''));}
    catch(error){handle(error,id);}finally{if(!signal.aborted)b.disabled=false;}
   }
   else if(a==='index'){
    const id=epoch;b.disabled=true;clearPoll();
    try{await request('learning.index_control',{paused:!paused});if(current(id))invalidate();}
    catch(error){handle(error,id);if(current(id)){b.disabled=false;if(!snapshot)schedule();}}
   }
  };
  document.addEventListener('visibilitychange',()=>{if(document.hidden)clearPoll();else if(scope&&!snapshot&&!paused)void getSnapshot();},{signal});
  window.addEventListener('qiban-bili-account-change',({detail})=>{
   if(account!=null&&String(account)!==String(detail.uid))reset();
  },{signal});
  await directory();
 });
})();
