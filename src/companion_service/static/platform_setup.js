"use strict";
(() => {
  if(kind!=='admin'||document.body.dataset.authenticated!=='true')return;
  let snapshot=null,request=0,timer=null;
  const lines=id=>$(id).value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean);
  function reset(){request++;snapshot=null;clearTimeout(timer);$('#setup-form').hidden=true;$('#setup-probe').disabled=true;$('#setup-prepare').disabled=true;$('#setup-qq-token').value='';$('#setup-status').textContent='请检查所选平台的配置。';}
  function render(value,fill){
    snapshot=value;const qq=value.platform==='qq';
    $('#setup-form').hidden=false;$('#setup-qq-fields').hidden=!qq;$('#setup-qq-guide').hidden=!qq;$('#setup-wechat-fields').hidden=qq;
    $('#setup-probe').disabled=value.running_job||!value.configured;$('#setup-prepare').disabled=value.running_job;
    $('#setup-form button[type=submit]').disabled=value.running_job;
    const j=value.job;
    const states={running:'进行中',completed:'已完成',failed:'失败',interrupted:'已中断'};
    const stages={python_create:'创建Python环境',dependencies_check:'检查已有依赖',dependencies_install:'安装依赖',native_check:'检查已有桥接',native_build:'构建原生桥接',native_verify:'校验构建结果'};
    $('#setup-status').textContent=`${value.configured?'已找到配置':'尚未配置'}${qq?'；Token '+(value.token_saved?'已保存':'待填写'):''}${j?'；最近任务：'+(states[j.status]||j.status)+(j.stage?' · '+(stages[j.stage]||j.stage):'')+(j.error?'（'+j.error+'）':''):''}${j?.result?.verified?'；身份核验成功':''}${j?.result?.dependencies==='reused'?'；已有依赖已复用':''}${j?.result?.native==='reused'?'；已有桥接已复用':''}`;
    $('#setup-instructions').textContent=value.instructions||'';
    if(fill){
      const c=value.configuration||{};
      if(qq){$('#setup-qq-account').value=c.account||'';$('#setup-qq-http').value=c.http||'http://127.0.0.1:3000';$('#setup-qq-ws').value=c.ws||'ws://127.0.0.1:3001';$('#setup-qq-private').value=(c.private_targets||[]).join('\n');$('#setup-qq-groups').value=(c.groups||[]).join('\n');}
      else{const targets=c.target_hints||[{kind:'private',hint:c.private_hint},{kind:'group',hint:c.group_hint}];$('#setup-wechat-dirs').value=(c.account_dirs||[]).join('\n');$('#setup-wechat-existing').textContent=(c.target_hints?'已登记对象：':'旧格式登记（保存时按已核验身份迁移）：')+targets.filter(t=>t.hint).map(t=>(t.kind==='private'?'私聊 ':'群聊 ')+t.hint).join('；');$('#setup-wechat-private').value='';$('#setup-wechat-groups').value='';}
    }
  }
  async function load(fill=true){
    const current=++request,platform=$('#setup-platform').value;
    const value=await operate('platform_setup_status',{platform});if(current!==request)return;
    render(value,fill);clearTimeout(timer);
    if(value.running_job)timer=setTimeout(()=>load(false).catch(e=>notice(e.message)),1500);
  }
  async function run(action,settings={}){
    if(!snapshot)throw new Error('请先检查现有配置');
    await operate('platform_setup_run',{platform:snapshot.platform,action,settings,expected_version:snapshot.version});
    $('#setup-qq-token').value='';await load(false);
  }
  bind('#setup-platform',reset,'change');bind('#setup-load',()=>load());
  bind('#setup-prepare',()=>run('prepare'));bind('#setup-probe',()=>run('probe'));
  bind('#setup-form',()=>run('save',snapshot?.platform==='qq'?{
    account:$('#setup-qq-account').value.trim(),http:$('#setup-qq-http').value.trim(),ws:$('#setup-qq-ws').value.trim(),
    token:$('#setup-qq-token').value,private_targets:lines('#setup-qq-private'),groups:lines('#setup-qq-groups')
  }:{account_dirs:lines('#setup-wechat-dirs'),targets:[...lines('#setup-wechat-private').map(hint=>({kind:'private',hint})),...lines('#setup-wechat-groups').map(hint=>({kind:'group',hint}))]}),'submit');
})();
