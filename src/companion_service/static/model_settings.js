"use strict";
(() => {
  if(kind!=='admin'||document.body.dataset.authenticated!=='true')return;
  const labels={global:'全局默认',local:'本机会客室',bilibili:'B站',qq:'QQ',wechat:'微信'};
  let settings=null,models=[],revision=0;
  const option=(value,label)=>{const el=document.createElement('option');el.value=value;el.textContent=label;return el;};
  function efforts(value=''){
    const item=models.find(row=>row.model===$('#model-choice').value),select=$('#model-effort');
    select.replaceChildren(option('','使用模型默认档位'));
    for(const effort of item?.efforts||[])select.append(option(effort,effort));
    select.value=value;select.disabled=!item;
  }
  function scopeChanged(){
    if(!settings)return;
    const scope=$('#model-scope').value,choice=scope==='global'?settings.global:settings.overrides[scope];
    const select=$('#model-choice');select.replaceChildren(option('',scope==='global'?'跟随 Codex 配置':'继承全局默认'));
    for(const item of models)select.append(option(item.model,item.label));
    if(choice&&!models.some(row=>row.model===choice.model))select.append(option(choice.model,`${choice.model}（需刷新目录核验）`));
    select.value=choice?.model||'';efforts(choice?.effort||'');
  }
  function render(){
    const list=$('#model-effective');list.replaceChildren();
    for(const [scope,value] of Object.entries(settings.effective)){
      const card=document.createElement('article');card.className='task-item';
      const title=document.createElement('h3');title.textContent=labels[scope];
      const model=document.createElement('p');model.textContent=value.model||'宿主默认';
      const note=document.createElement('p');note.className='task-hint';note.textContent=`${value.effort||'默认档位'} · ${value.source==='codex_config'?'Codex 配置':labels[value.source]}`;
      card.append(title,model,note);list.append(card);
    }
    $('#model-settings-form').hidden=false;scopeChanged();
  }
  async function load(){
    const request=++revision;
    const value=await operate('model_settings_get',{});if(request!==revision)return;
    settings=value;render();$('#model-state').textContent='已读取。分平台覆盖优先于全局默认；保存只影响之后的模型请求。';
  }
  bind('#model-load',load);
  bind('#model-refresh',async()=>{
    const request=++revision;$('#model-state').textContent='正在向宿主查询可选模型…';
    try{
      const [catalog,value]=await Promise.all([operate('model_catalog',{}),operate('model_settings_get',{})]);
      if(request!==revision)return;models=catalog.items;settings=value;render();
      $('#model-state').textContent=`已读取 ${models.length} 个可选模型。保存时还会再次核验。`;
    }catch(error){if(request===revision)$('#model-state').textContent='模型目录读取失败，已有设置仍保留。';throw error;}
  });
  bind('#model-scope',scopeChanged,'change');
  bind('#model-choice',()=>efforts(),'change');
  bind('#model-settings-form',async()=>{
    if(!settings)throw new Error('请先读取模型设置');
    const request=++revision;
    const args={scope:$('#model-scope').value,model:$('#model-choice').value||null,
      effort:$('#model-effort').value||null,expected_version:settings.version};
    if(args.model===null)args.effort=null;
    try{
      const value=await operate('model_settings_set',args);if(request!==revision)return;
      settings=value;render();$('#model-state').textContent='已保存，从下一次模型请求生效；当前请求保持原型号。';
    }catch(error){if(request===revision)$('#model-state').textContent='保存未成功，请重新读取设置后检查。';throw error;}
  },'submit');
})();
