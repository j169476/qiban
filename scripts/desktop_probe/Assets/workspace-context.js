(() => {
 'use strict';
 const ui=window.QibanWorkspace,{escape:e,option,button,label}=ui;
 const {catalog,field,help,replies}=window.QibanPresets;
 const layers={
  memory_records:'让龙娘参考已经记下的事情。',summary:'把之前的聊天摘要带入回复。',
  relations:'让龙娘参考已知的人物关系。',growth:'让龙娘参考这个场景的成长记录。',
  catalog:'让龙娘知道有哪些资料可查。',files:'允许参考此会话已获准使用的文件。',stickers:'允许参考此会话已获准使用的表情。'
 };
 const limits={reply_context_chars:'每次回复最多参考多少字的资料。',reply_context_tokens:'设置本次回复可用的总额度。',reply_output_reserve:'从总额度中留给龙娘写回复的份额。'};
 const panels={memory_records:['persona','memory','memory-panel'],catalog:['persona','memory','memory-panel'],
  relations:['persona','relations','relation-claims-panel'],files:['media','library','file-panel'],stickers:['media','stickers','sticker-panel']};
 const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
 const copy=structuredClone;
 const uid=()=>crypto.randomUUID().replaceAll('-','');
 function budget(value){
  if(!value)return '<p>暂无预算记录。</p>';
  return '<p>字数 '+e(value.before?.chars??'—')+' → '+e(value.after?.chars??'—')+' · 估算 token '+e(value.after?.estimated_tokens??'—')+
   ' / '+e(value.limits?.reply_context_tokens??'—')+' · 输出预留 '+e(value.limits?.reply_output_reserve??'—')+'</p>'+
   Object.entries(value.sections||{}).map(([id,row])=>'<div class="ws-row"><span>'+e(id==='attachment_text'?'附件摘录':label(id))+'</span><span>入选 '+
    e(row.kept??'—')+' / '+e(row.offered??'—')+(row.duplicates?' · 重复 '+e(row.duplicates):'')+(row.skipped?' · 跳过 '+e(row.skipped):'')+
    (row.reason?' · '+e(({disabled:'已停用',budget:'预算不足'})[row.reason]||row.reason):'')+(row.limited?' · 仅部分纳入':'')+'</span></div>').join('');
 }
 // Keep changes to fields the user edited; other fields come from the newly read version.
 function reconcile(base,local,fresh){
  const merged=copy(fresh);
  for(const group of ['guidance','limits']){
   for(const key of new Set([...Object.keys(base[group]),...Object.keys(local[group])])){
    if(base[group][key]===local[group][key])continue;
    if(key in local[group])merged[group][key]=local[group][key];else delete merged[group][key];
   }
  }
  if(!same(base.order,local.order))merged.order=copy(local.order);
  const off=new Set(fresh.disabled||[]);
  for(const id of local.order){
   const on=(local.disabled||[]).includes(id);
   if(on!==(base.disabled||[]).includes(id)){if(on)off.add(id);else off.delete(id);}
  }
  if(off.size)merged.disabled=merged.order.filter(id=>off.has(id));else delete merged.disabled;
  return merged;
 }
 ui.register('context',async({body,signal,has,request,drafts})=>{
  const plans=catalog(request),canList=has('context.presets')&&has('context.preset_read'),
   canSave=canList&&has('context.preset_save'),canApply=canList&&has('context.preset_apply');
  const view=await request('context.workspace');
  let def,active=view.active;
  if(canList){await plans.list();def=await request('context.preset_read',{id:'default'});plans.put(def);}
  if(signal.aborted)return;
  if(active.preset)plans.put(plans.get(active.preset.id)||active.preset);
  let selected=active.preset?.id||def?.id||'',scene=view.scenes[0],edit,config,name,newId,busy='',result='',conflict=null,applyMessage='',editMessage='';
  function load(p,newPlan=false){edit=copy(p);if(newPlan){edit.id=null;edit.name='';edit.revision=null;}
   config=copy(edit.config);name=edit.name;newId=uid();conflict=null;result='';editMessage='';}
  load({id:active.preset?.id||null,name:active.preset?.name||'',revision:active.preset?.revision||null,config:active.config});
  const kept=drafts.get('context');
  if(kept&&canSave){({edit,config,name,newId,conflict}=kept);if(view.scenes.includes(kept.scene))scene=kept.scene;
   if(kept.choice){if(!plans.get(kept.choice.id))plans.put(kept.choice);selected=kept.choice.id;}}
  signal.addEventListener('abort',()=>{
   if(canSave)drafts.set('context',{edit:copy(edit),config:copy(config),name,newId,conflict:copy(conflict),choice:plans.get(selected),scene});
  },{once:true});
  const dirty=()=>!same(edit.config,config)||edit.name!==name;
  const chosen=()=>plans.get(selected);
  const q=sel=>body.querySelector(sel);
  function layerRow(id,index){
   const key='ws-layer-'+id;
   const supported=Object.hasOwn(panels,id)||(['summary','growth'].includes(id)&&has('context.source_list')&&has('context.source_detail'));
   return '<li><div class="ws-layer-label"><label for="'+e(key)+'"><input id="'+e(key)+'" aria-describedby="'+e(key)+'-tip" type="checkbox" data-layer="'+e(id)+'" '+
    ((config.disabled||[]).includes(id)?'':'checked')+(!canSave||busy?' disabled':'')+'>'+e(label(id))+'</label>'+help(label(id),layers[id]||'选择后，这类资料可以参与回复。',key+'-tip')+
    '</div><div class="ws-layer-actions"><button type="button" class="secondary" data-source="'+e(id)+'" aria-label="修改'+e(label(id))+'" '+
    (supported&&!busy?'':'disabled')+'>修改</button>'+button('up-'+index,'上移',!canSave||!!busy||index===0)+button('down-'+index,'下移',!canSave||!!busy||index===config.order.length-1)+'</div></li>';
  }
  function render(){
   if(signal.aborted)return;
   const off=busy?' disabled':'',readonly=canSave?off:' disabled';
   body.innerHTML='<section class="ws-plan-card"><h3>配置方案</h3><div class="ws-plan-picker">'+
    field('方案选择','选择要应用、加载或查看回复的方案。','<select data-preset-select'+(canList?off:' disabled')+'>'+plans.choices(selected)+'</select>')+
    '<div class="ws-plan-actions"><span class="ws-apply-group">'+button('preset-use',busy==='apply'?'应用中…':'应用方案',!canApply||!!busy||!chosen())+'<span role="status" data-apply-note>'+e(applyMessage)+'</span></span>'+
    button('preset-default','恢复默认',!canApply||!!busy)+button('traces','回复记录',!chosen()||!has('context.preset_replies')||!!busy)+'</div></div></section>'+
    '<section class="ws-plan-editor"><h3>方案制作工作台</h3><div class="ws-editor-tools"><div class="ws-actions">'+
    button('preset-new','新建',!canSave||!!busy)+button('preset-load',busy==='load'?'加载中…':'加载',!canList||!!busy||!chosen())+
    button('preset-save',busy==='save'?'保存中…':'保存',!canSave||!!busy||!!conflict||!!edit.id&&!dirty())+button('preview',busy==='preview'?'试算中…':'试算草稿',!!busy||!has('context.preview_config'))+'</div>'+
    field('方案名称','给方案起个方便查找的名字。','<input data-preset-name maxlength="80"'+readonly+' value="'+e(name)+'">')+'</div>'+
    '<p class="ws-inline-note" data-edit-note role="status">'+e(editMessage)+'</p><div class="ws-conflict">'+(conflict?'<p>这份方案已在别处更新。选择要继续编辑的内容。</p>'+button('reapply','带回我的修改')+button('discard','使用最新方案'):'')+'</div>'+
    '<div class="ws-result" aria-live="polite">'+result+'</div><div class="ws-editor-block"><h4>场景与预算</h4>'+
    field('使用场景','选择要调整和试算的聊天场景。','<select data-field="scene"'+off+'>'+view.scenes.map(s=>option(s,label(s),s===scene)).join('')+'</select>')+
    field('应用安全对齐','写下龙娘在这个场景回复时要遵守的要求。','<textarea data-field="guidance" rows="3" maxlength="2000"'+readonly+'>'+e(config.guidance[scene]||'')+'</textarea>')+
    '<div class="ws-limit-grid">'+Object.entries(limits).map(([k,h])=>field(label(k),h,'<input data-limit="'+k+'" type="number" step="1" min="0" placeholder="沿用角色设置"'+readonly+' value="'+e(config.limits[k]??'')+'">')).join('')+'</div></div>'+
    '<div class="ws-editor-block"><h4>已获准的资料顺序</h4><ol class="ws-order">'+config.order.map(layerRow).join('')+'</ol></div></section>';
  }
  async function run(kind,fn){
   if(busy||signal.aborted)return;
   const trigger=document.activeElement?.dataset.ws;
   busy=kind;render();
   try{await fn();}
   catch(error){if(!signal.aborted){if(kind==='apply'||kind==='default')applyMessage=error.message;else editMessage=error.message;}}
   finally{
    busy='';render();
    if(!signal.aborted&&trigger&&document.activeElement===document.body)q('[data-ws="'+CSS.escape(trigger)+'"]:enabled')?.focus({preventScroll:true});
   }
  }
  function confirmReplace(next){
   if(!dirty()){next();return;}
   const p=window.QibanSources.panel('替换当前编辑？',signal);if(!p)return;p.body.hidden=true;
   void p.ask('替换当前编辑？','替换').then(ok=>{p.close();if(ok&&!signal.aborted)next();});
  }
  async function apply(id){
   await run(id==='default'&&id!==selected?'default':'apply',async()=>{
    applyMessage='';
    try{
     const item=plans.get(id),answer=await request('context.preset_apply',{id,revision:item.revision,base:active.version});
     if(signal.aborted)return;
     active=answer.workspace.active;plans.put(answer.preset);selected=id;applyMessage=id==='default'?'已恢复默认':'已应用';
     window.dispatchEvent(new CustomEvent('qiban-context-applied',{detail:active.preset}));
    }catch(error){
     if(['edit_conflict','workspace_version','timeout'].includes(error.code)){
      const [v,p]=await Promise.all([request('context.workspace'),request('context.preset_read',{id})]);
      if(signal.aborted)return;
      active=v.active;plans.put(p);applyMessage='已重新读取，请核对后再应用。';
     }else throw error;
    }
   });
  }
  function save(){
   if(!name.trim()){q('[data-edit-note]').textContent='请填写方案名称。';q('[data-preset-name]').focus();return;}
   if(!valid())return;
   void run('save',async()=>{
    const existing=edit.id&&edit.id!=='default',id=existing?edit.id:newId;
    try{
     const answer=await request('context.preset_save',{id,revision:existing?edit.revision:null,name:name.trim(),config:copy(config)});
     if(signal.aborted)return;
     if(answer.workspace?.active)active=answer.workspace.active;
     plans.put(answer.preset);selected=answer.preset.id;applyMessage='';load(answer.preset);editMessage='已保存。';
    }catch(error){
     if(['edit_conflict','timeout'].includes(error.code)){
      try{
       const fresh=await request('context.preset_read',{id});
       if(signal.aborted)return;
       if(fresh.name===name.trim()&&same(fresh.config,config)){plans.put(fresh);selected=id;load(fresh);editMessage='已保存。';return;}
       conflict=fresh;plans.put(fresh);editMessage='你的输入已保留。';return;
      }catch{/* A timed-out new save may not exist yet; retain its id for a retry. */}
     }
     throw error;
    }
   });
  }
  function valid(){
   for(const input of body.querySelectorAll('input[type=number]'))if(!input.reportValidity())return false;
   return true;
  }
  function modified(){
   result='';editMessage='';q('.ws-result').replaceChildren();q('[data-edit-note]').textContent='';
   q('[data-ws=preset-save]').disabled=!canSave||!!conflict||!!edit.id&&!dirty();
  }
  body.oninput=({target:t})=>{
   if(busy||!canSave)return;
   if(t.hasAttribute('data-preset-name'))name=t.value;
   else if(t.dataset.field==='guidance'){if(t.value)config.guidance[scene]=t.value;else delete config.guidance[scene];}
   else if(t.dataset.limit){if(t.value==='')delete config.limits[t.dataset.limit];else config.limits[t.dataset.limit]=Number(t.value);}
   else if(t.dataset.layer){const off=new Set(config.disabled||[]);if(t.checked)off.delete(t.dataset.layer);else off.add(t.dataset.layer);
    if(off.size)config.disabled=config.order.filter(k=>off.has(k));else delete config.disabled;}
   else return;modified();
  };
  body.onchange=({target:t})=>{
   if(busy)return;
   if(t.hasAttribute('data-preset-select')){
    if(t.value==='@more'){void run('list',()=>plans.list(true));return;}
    selected=t.value;applyMessage='';render();q('[data-preset-select]').focus();
   }else if(t.dataset.field==='scene'){scene=t.value;modified();render();q('[data-field=scene]').focus();}
  };
  body.onclick=ev=>{
   const b=ev.target.closest('button');if(!b||b.disabled||busy)return;
   const a=b.dataset.ws;
   if(a==='preset-use')void apply(selected);
   else if(a==='preset-default')void apply('default');
   else if(a==='traces')replies({catalog:plans,selected,signal});
   else if(a==='preset-new')confirmReplace(()=>{load(def,true);render();q('[data-preset-name]').focus();});
   else if(a==='preset-load')confirmReplace(()=>{void run('load',async()=>{const v=await request('context.preset_read',{id:selected});plans.put(v);load(v);editMessage='已加载。';});});
   else if(a==='preset-save')save();
   else if(a==='preview'&&valid())void run('preview',async()=>{
    const v=await request('context.preview_config',{scene,config:copy(config)});
    result='<h4>'+e(v.source==='synthetic'?'合成场景试算':'场景试算')+' · '+e(label(v.scene))+'</h4><div class="ws-columns"><section><h4>当前使用</h4>'+budget(v.reports.active)+
     '</section><section><h4>本次编辑</h4>'+budget(v.reports.draft)+'</section></div>'+(v.excluded||[]).map(x=>'<p>'+e(label(x.layer))+' · '+e(label(x.reason))+'</p>').join('');
    editMessage='试算完成。';
   });
   else if(a==='reapply'||a==='discard'){
    const merged=a==='reapply'?reconcile(edit.config,config,conflict.config):conflict.config;
    const nextName=a==='reapply'&&name!==edit.name?name:conflict.name;load(conflict);config=merged;name=nextName;render();
   }else if(/^(up|down)-\d+$/.test(a)){
    const i=Number(a.split('-')[1]),j=i+(a.startsWith('up')?-1:1);
    [config.order[i],config.order[j]]=[config.order[j],config.order[i]];modified();render();q('[data-layer="'+CSS.escape(config.order[j])+'"]').focus();
   }else if(b.dataset.source){
    const id=b.dataset.source;
    if(panels[id]){drafts.set('source-return',id);window.QibanSources.openPanel(...panels[id],id);}
    else window.QibanSources.openEditor({layer:id,signal,has,request});
   }
  };
  render();
  const back=drafts.get('source-return');
  if(back){drafts.delete('source-return');q('[data-source="'+CSS.escape(back)+'"]')?.focus();}
 });
})();
