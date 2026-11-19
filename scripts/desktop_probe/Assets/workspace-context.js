(() => {
 'use strict';
 const ui=window.QibanWorkspace,{escape:e,option,button,label}=ui;
 ui.register('context',async({body,signal,caps,has,request,notice,showError})=>{
  let view,config,scene,pane='edit',busy=false,confirm='',history=[],after=null;
  let result='',readSeq=0,writesPaused=false,conflicts=[];
  const limits=['reply_context_chars','reply_context_tokens','reply_output_reserve'];
  const short=v=>v==='initial'?'初始版':String(v??'').slice(0,8);
  const version=v=>'<span title="'+e(v)+'">'+e(short(v))+'</span>';
  const dirty=()=>JSON.stringify(config)!==JSON.stringify(view.draft.config);
  const changed=()=>JSON.stringify(view.draft.config)!==JSON.stringify(view.active.config);
  const opt=(op,id,name,disabled=false)=>has(op)?button(id,name,busy||disabled||(writesPaused&&['context.save','context.publish','context.rollback'].includes(op))):'';
  function assign(value){
   view=value;config=structuredClone(view.draft.config);
   if(!view.scenes.includes(scene))scene=view.scenes[0];confirm='';
  }
  function changedFields(old,edited){
   const fields=[];
   for(const group of ['guidance','limits']){
    for(const key of new Set([...Object.keys(old[group]),...Object.keys(edited[group])])){
     if(old[group][key]!==edited[group][key])fields.push({group,key,value:edited[group][key]});
    }
   }
   if(JSON.stringify(old.order)!==JSON.stringify(edited.order))fields.push({key:'order',value:edited.order});
   for(const key of edited.order){
    if((old.disabled||[]).includes(key)!==(edited.disabled||[]).includes(key))fields.push({group:'disabled',key,value:(edited.disabled||[]).includes(key)});
   }
   return fields;
  }
  const fieldValue=(conf,f)=>f.group==='disabled'?(conf.disabled||[]).includes(f.key):f.group?conf[f.group][f.key]:conf[f.key];
  const describe=v=>Array.isArray(v)?v.map(label).join('、'):typeof v==='boolean'?(v?'已停用':'已启用'):String(v??'继承默认');
  function disableLayer(id,disabled){
   const keys=new Set(config.disabled||[]);if(disabled)keys.add(id);else keys.delete(id);
   if(keys.size)config.disabled=config.order.filter(k=>keys.has(k));else delete config.disabled;
  }
  function buttons(){
   const bar=body.querySelector('.ws-savebar');
   if(!bar)return;
   bar.innerHTML=opt('context.save','save','保存草稿',!dirty()||!!conflicts.length)+
    opt('context.publish','publish','发布草稿',dirty()||!changed()||!!conflicts.length)+
    opt('context.preview','preview','试算草稿',dirty()||!scene)+
    '<span>'+e(dirty()?'有未保存的修改':changed()?'草稿待发布':'与生效版一致')+'</span>';
  }
  function render(){
   if(signal.aborted)return;
   const tabs=button('edit','草稿')+(writesPaused?button('reload','重新读取'): '')+opt('context.history','history','版本记录')+
    (has('reply_trace.detail')?button('traces','真实回复记录',busy):'');
   const heading='<div class="ws-meta">生效 '+version(view.active.version)+' · 草稿 '+version(view.draft.revision)+
    (caps.context_scope==='installation'?' · 本机角色':'')+'</div><div class="ws-meta">'+e((caps.context_routes||[]).map(label).join('、'))+'</div>';
   body.innerHTML='<div class="ws-actions">'+tabs+'</div>'+heading+'<div class="ws-conflict"></div><div class="ws-main"></div><div class="ws-confirm"></div><div class="ws-result"></div>';
   if(conflicts.length){
    body.querySelector('.ws-conflict').innerHTML='<h3>核对未保存的修改</h3>'+conflicts.map(f=>'<p><strong>'+e(f.key==='order'?'资料顺序':label(f.key))+'</strong><br>当前草稿　'+e(describe(fieldValue(config,f)))+'<br>上次输入　'+e(describe(f.value))+'</p>').join('')+button('reapply','带回这几项修改')+button('discard','使用当前草稿');
   }
   const main=body.querySelector('.ws-main');
   if(pane==='edit'){
    main.innerHTML='<fieldset '+(busy||!has('context.save')?'disabled':'')+'><legend>应用规则与预算</legend><label>使用场景<select data-field="scene">'+view.scenes.map(s=>option(s,label(s),s===scene)).join('')+'</select></label><label>应用安全对齐<textarea data-field="guidance" rows="3">'+e(config.guidance[scene]||'')+'</textarea></label>'+
     '<div class="ws-fields">'+limits.map(k=>'<label>'+e(label(k))+'<input data-limit="'+k+'" type="number" step="1" placeholder="继承角色配置" value="'+e(config.limits[k]??'')+'"></label>').join('')+'</div>'+
     '<h3>已获准的资料顺序</h3><ol class="ws-order">'+config.order.map((id,i)=>'<li><label><input type="checkbox" data-layer="'+e(id)+'" '+((config.disabled||[]).includes(id)?'':'checked')+'>'+e(label(id))+'</label>'+button('up-'+i,'上移',!i)+button('down-'+i,'下移',i===config.order.length-1)+'</li>').join('')+'</ol></fieldset><div class="ws-actions ws-savebar"></div>';
    // Read-only hosts may still compare any supported scene.
    if(!has('context.save')||busy){
     const pick=main.querySelector('[data-field=scene]');
     if(!busy&&view.scenes.length>1){const sceneLabel=pick.closest('label');main.prepend(sceneLabel);}
    }
    buttons();
   }else if(pane==='history'){
    main.innerHTML='<h3>版本记录</h3>'+history.map(h=>'<div class="ws-row"><span>'+version(h.version)+' · '+e(h.at?new Date(h.at).toLocaleString():'初始配置')+(h.actor?' · '+e(h.actor):'')+'</span>'+
     (h.version!==view.active.version&&has('context.rollback')?'<button class="secondary" data-version="'+e(h.version)+'" '+(busy||writesPaused||dirty()?'disabled':'')+'>恢复此版</button>':'')+'</div>').join('')+(after?button('more-history','更多版本',busy):'');
   }
   body.querySelector('.ws-result').innerHTML=result;
   if(confirm){
    const action=confirm==='publish'?'发布草稿':'恢复此版';
    body.querySelector('.ws-confirm').innerHTML='<p>确认'+action+'？下一次回复将使用新版本。</p>'+button('accept','确认'+action,busy)+button('cancel','取消',busy);
   }
  }
  function report(r){
   if(!r)return '<p>未提供预算记录。</p>';
   const used=r.after||{},max=r.limits||{};
   return '<p>字数 '+e(r.before?.chars??'—')+' → '+e(used.chars??'—')+' · 估算 token '+e(used.estimated_tokens??'—')+' / '+e(max.reply_context_tokens??'—')+' · 输出预留 '+e(max.reply_output_reserve??'—')+'</p>'+
    Object.entries(r.sections||{}).map(([key,s])=>'<div class="ws-row"><span>'+e(key==='attachment_text'?'附件摘录':label(key))+'</span><span>入选 '+e(s.kept??'—')+' / '+e(s.offered??'—')+
     (s.duplicates!=null?' · 重复 '+e(s.duplicates):'')+(s.skipped?' · 跳过 '+e(s.skipped):'')+(s.reason?' · '+e(({disabled:'已停用',budget:'预算不足'})[s.reason]||s.reason):'')+(s.limited?' · 仅部分纳入':'')+'</span></div>').join('');
  }
  async function read(fn){
   const seq=++readSeq;confirm='';notice('正在读取…');
   try{const paint=await fn();if(signal.aborted||seq!==readSeq)return;paint();notice('');render();}
   catch(error){if(seq===readSeq){if(['edit_conflict','workspace_version'].includes(error.code)){pane='edit';await recover();}else showError(error);}}
  }
  async function recover(){
   const local=dirty()?changedFields(view.draft.config,config):conflicts;
   try{assign(await request('context.workspace'));writesPaused=false;
    conflicts=local.filter(f=>JSON.stringify(fieldValue(config,f))!==JSON.stringify(f.value));
    result='';notice(conflicts.length?'已读取当前草稿，请核对上次输入。':'已重新读取生效版与草稿。');render();}
   catch(err){showError(err);}
  }
  async function write(op,args){
   if(busy||writesPaused)return;
   ++readSeq;busy=true;confirm='';result='';render();notice('正在提交…');
   try{assign(await request(op,{base:view.active.version,expected:view.draft.revision,...args}));
    pane='edit';notice(op==='context.save'?'草稿已保存。':'生效版本已更新。');}
   catch(error){
    if(error.code==='edit_conflict'||error.code==='timeout')await recover();
    else if(error.code==='workspace_version'){pane='edit';await recover();}
    else{if(['locked','restore_pending'].includes(error.code))writesPaused=true;showError(error);}
   }
   finally{busy=false;render();}
  }
  function openHistory(more){
   return read(async()=>{const h=await request('context.history',{after:more?after:null});return ()=>{
    pane='history';result='';history=more?[...history,...h.items]:h.items;after=h.after;
   };});
  }
  body.oninput=({target:t})=>{
   if(busy)return;
   if(t.dataset.field==='guidance'){if(t.value)config.guidance[scene]=t.value;else delete config.guidance[scene];}
   else if(t.dataset.layer)disableLayer(t.dataset.layer,!t.checked);
   else if(t.dataset.limit){if(t.value==='')delete config.limits[t.dataset.limit];else config.limits[t.dataset.limit]=Number(t.value);}
   else return;
   confirm='';++readSeq;result='';body.querySelector('.ws-result').replaceChildren();body.querySelector('.ws-confirm').replaceChildren();buttons();
  };
  body.onchange=({target:t})=>{if(t.dataset.field==='scene'){++readSeq;scene=t.value;confirm='';result='';notice('');render();}};
  body.onclick=event=>{
   const b=event.target.closest('button');if(!b||b.disabled||busy)return;
   const a=b.dataset.ws||'';
   if(a==='discard'||a==='reapply'){
    if(a==='reapply')for(const f of conflicts){
     if(f.group==='disabled')disableLayer(f.key,f.value);
     else if(f.group){if(f.value===undefined)delete config[f.group][f.key];else config[f.group][f.key]=f.value;}
     else config[f.key]=structuredClone(f.value);
    }
    conflicts=[];notice('');render();return;
   }
   if(b.dataset.version){confirm=b.dataset.version;render();return;}
   if(b.dataset.trace){
    void read(async()=>{const t=await request('reply_trace.detail',{identifier:b.dataset.trace});
     const step=(t.steps||[]).find(s=>s.stage==='context_budget');
     const reason=window.QibanTraceResult.reason(t);
     return ()=>{result='<h3>真实回复 · '+e(window.QibanTraceResult.label(t))+'</h3>'+(reason?'<p>'+e(reason)+'</p>':'')+(step?'<p>配置版本 '+version(step.workspace_version||'—')+'</p>'+report(step):'<p>本次记录没有预算步骤。</p>')+
       (t.sources||[]).map(s=>'<p>'+e(s.resource)+' · '+e(s.version)+(s.version_current===false?' · 来源已更新':'')+'</p>').join('')+button('traces','返回记录');};});return;
   }
   if(a==='edit'){++readSeq;pane='edit';result='';confirm='';notice('');render();return;}
   if(a==='reload'){void recover();return;}
   if(a==='history'||a==='more-history'){void openHistory(a==='more-history');return;}
   if(a==='traces'){void read(async()=>{const traces=await request('reply_trace.list',{limit:30});return ()=>{
    pane='traces';result='<h3>真实回复记录</h3>'+(traces.items.length?traces.items.map(t=>'<button class="secondary ws-row" data-trace="'+e(t.id)+'">'+e(t.started?new Date(t.started).toLocaleString():t.id)+' · '+e(label(t.platform))+' · '+e(window.QibanTraceResult.label(t))+'</button>').join(''):'<p>暂无记录。</p>');};});return;}
   if(/^(up|down)-\d+$/.test(a)){const i=Number(a.split('-')[1]),j=i+(a.startsWith('up')?-1:1);[config.order[i],config.order[j]]=[config.order[j],config.order[i]];confirm='';result='';++readSeq;render();return;}
   if(a==='save'){void write('context.save',{config});return;}
   if(a==='publish'){confirm='publish';render();return;}
   if(a==='cancel'){confirm='';render();return;}
   if(a==='accept'&&confirm){const target=confirm;void write(target==='publish'?'context.publish':'context.rollback',target==='publish'?{}:{version:target});return;}
   if(a==='preview'){void read(async()=>{const p=await request('context.preview',{scene,expected:view.draft.revision});return ()=>{
    result='<h3>'+e(p.source==='synthetic'?'合成场景试算':'场景试算')+' · '+e(label(p.scene))+'</h3><div class="ws-columns"><section><h4>生效版</h4>'+report(p.reports.active)+'</section><section><h4>草稿</h4>'+report(p.reports.draft)+'</section></div>'+
     (p.excluded||[]).map(x=>'<p>'+e(label(x.layer))+' · '+e(label(x.reason))+'</p>').join('');};});}
  };
  assign(await request('context.workspace'));render();
 });
})();
