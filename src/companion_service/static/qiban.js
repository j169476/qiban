"use strict";
(() => {
  const authenticated = document.body.dataset.authenticated === "true";
  const admin = kind === "admin" && authenticated;
  const pages = {
    dashboard: ["我的栖伴", "所有陪伴，在这里相遇。"],
    platforms: ["平台与会话", "把每一段联系，照顾妥帖。"],
    memories: ["角色与记忆", "珍藏经历，也尊重每一份隐私。"],
    tasks: ["任务手记", "正在做的事，都有迹可循。"],
    usage: ["额度与用量", "清楚每一份消耗，从容安排陪伴。"],
    settings: ["系统设置", "为栖伴，布置一个安心的家。"]
  };
  const names = {local:"本机会客室",bilibili:"哔哩哔哩",wechat:"微信",qq:"QQ"};
  const phases = {running:"进行中",queued:"等待执行",success:"完成",completed:"已完成",failed:"未完成",cancelled:"已取消",paused:"已暂停",active:"运行中",start:"准备开始",research:"资料检索",write:"整理内容"};
  const reasons = {explicit_start_required:"等待你开启陪伴",service_restart:"服务已启动，等待明确开启",host_stopped:"ChatGPT已停止运行，请重启服务",outside_active_hours:"等待设定的活动时间",platform_unavailable:"连接器尚未就绪",chat_archived:"归属聊天已归档",admin_web_stop:"已在管理页暂停",explicit_stop:"已按指令停止",duration_complete:"本次运行时间已结束",explicit_service_resume:"服务已重启，请选择要启动的平台"};
  const format = value => typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat("zh-CN").format(value) : "—";
  const time = value => typeof value === "number" ? new Date(value*1000).toLocaleString("zh-CN",{hour12:false}) : "未知";
  const text = (selector,value) => { const el=$(selector); if(el) el.textContent=value; };
  function element(tag, className, value) {const el=document.createElement(tag);if(className)el.className=className;if(value!==undefined)el.textContent=value;return el;}
  function icon(name) {
    const svg=document.createElementNS("http://www.w3.org/2000/svg","svg"),use=document.createElementNS("http://www.w3.org/2000/svg","use");
    svg.classList.add("icon");svg.setAttribute("aria-hidden","true");use.setAttribute("href","/static/icons.svg#"+name);svg.append(use);return svg;
  }
  function empty(container,title,message) {
    container.replaceChildren();const box=element("div","soft-empty");box.append(icon("moon"),element("h3","",title),element("p","",message));container.append(box);
  }
  function route(id) {
    if(!admin || !pages[id])return;
    document.querySelectorAll("[data-panel]").forEach(el=>{el.hidden=el.dataset.panel!==id;});
    document.querySelectorAll("[data-nav]").forEach(el=>{if(el.dataset.nav===id)el.setAttribute("aria-current","page");else el.removeAttribute("aria-current");});
    text("#page-title",pages[id][0]);text("#page-subtitle",pages[id][1]);document.title=pages[id][0]+" · 栖伴 Qiban";
  }
  window.qibanNavigate=id=>{if(pages[id]){location.hash=id;route(id);}};
  if(admin){
    route(location.hash.slice(1) in pages ? location.hash.slice(1) : "dashboard");
    window.addEventListener("hashchange",()=>route(location.hash.slice(1)));
    document.querySelectorAll("[data-go]").forEach(button=>button.addEventListener("click",()=>window.qibanNavigate(button.dataset.go)));
  }
  let platformSignature="", reportData=null, loading=false;
  window.qibanSessionExpired=()=>{
    const banner=$("#host-banner");if(!banner)return;banner.hidden=false;banner.classList.remove("disconnected");
    text("#host-banner-title","管理页登录已过期");text("#host-banner-message","请刷新页面并重新登录。登录过期不会停止后台平台，也不表示本机服务已断开。");
    text("#status","请重新登录");$("#status")?.classList.remove("active");
    document.querySelectorAll("[data-runtime-start]").forEach(button=>button.disabled=true);platformSignature="";
  };
  window.qibanConnectionLost=()=>{
    const banner=$("#host-banner");if(!banner)return;banner.hidden=false;banner.classList.add("disconnected");
    text("#host-banner-title","暂时无法连接本机服务");text("#host-banner-message","本机服务已断开，暂时无法确认宿主与任务状态，也无法保存配置。退出 Codex 可能同时结束后台；重新打开 Codex 后，请在管理聊天或命令行明确重启服务。");
    text("#status","连接中断");$("#status")?.classList.remove("active");
    document.querySelectorAll("[data-runtime-start]").forEach(button=>button.disabled=true);
    const send=$("#message button[type=submit]");if(send)send.disabled=true;
    platformSignature="";
  };
  function hostBanner(state) {
    const host=state.host||{},banner=$("#host-banner");if(!banner)return;
    const stopped=host.available===false||host.restart_required;
    banner.hidden=!stopped;banner.classList.remove("disconnected");
    text("#host-banner-title",host.available===false?"ChatGPT已停止运行":host.restart_required?"服务等待明确重启":"");
    text("#host-banner-message","平台运行保持禁用。请确认 Codex 已打开，再在管理聊天或命令行明确重启服务；恢复后仍需单独启动平台。");
    text("#host-detail",stopped?"等待明确重启服务":"Codex 宿主在线 · 本机服务可用");
    if(stopped){text("#status","等待重启");$("#status")?.classList.remove("active");}
  }
  const disconnected=row=>row.status==="active"&&row.connection&&row.connection.state!=="connected";
  function runtimeLabel(row){return !row.available?"未接入":disconnected(row)?(row.connection.state==="blocked"?"连接受阻":"等待重连"):row.status==="active"?(row.busy?"处理中":"运行中"):"已暂停";}
  function runtimeDescription(row){return !row.available?"完成连接器配置后，即可在这里管理。":disconnected(row)?"平台连接尚未恢复，请检查客户端登录与连接器。"+(row.connection.code?"（"+row.connection.code+"）":""):reasons[row.reason]||(row.status==="active"?"陪伴正在继续。":"等待你的下一次启动。");}
  function runtimeCards(state) {
    const list=[{platform:"local",available:true,...state},...(state.platforms||[])];
    const signature=JSON.stringify(list.map(p=>[p.platform,p.available,p.status,p.reason,p.owner,p.generation,p.busy,p.connection,state.host]));
    if(signature===platformSignature)return;platformSignature=signature;
    const grid=$("#platform-grid");if(!grid)return;
    // Keep edited options through background refreshes; never replace controls
    // while a user is focusing them.
    if(grid.contains(document.activeElement)){
      for(const row of list){const card=grid.querySelector(`[data-platform="${row.platform}"]`);if(!card)continue;const badge=card.querySelector('.badge');badge.textContent=runtimeLabel(row);badge.classList.toggle('active',row.status==='active'&&!disconnected(row));card.querySelector('.platform-description').textContent=runtimeDescription(row);card.querySelector('[data-runtime-start]').disabled=!row.available||row.status==='active'||state.host?.available===false||state.host?.restart_required===true;card.querySelector('[data-runtime-stop]').disabled=row.status!=='active';}
      if(state.host?.available===false||state.host?.restart_required)grid.querySelectorAll("[data-runtime-start]").forEach(button=>button.disabled=true);
      platformSignature="";return;
    }
    const saved=new Map([...grid.querySelectorAll(".platform-card")].map(card=>[card.dataset.platform,{duration:card.querySelector("input[type=number]")?.value,readonly:card.querySelector("input[type=checkbox]")?.checked}]));
    grid.replaceChildren();
    for(const row of list){
      const card=element("article","platform-card"),head=element("div","section-heading"),title=element("h3"),mark=element("span","platform-mark");card.dataset.platform=row.platform;
      mark.append(icon(row.platform==="local"?"cup":"platform"));title.append(mark,document.createTextNode(names[row.platform]));
      const badge=element("span","badge"+(row.status==="active"&&!disconnected(row)?" active":""),runtimeLabel(row));head.append(title,badge);
      card.append(head,element("p","platform-description",runtimeDescription(row)));
      card.append(element("p","owner",row.owner?"归属聊天 · "+row.owner:"网页启动后归属已绑定的管理聊天"));
      if(row.reply_test&&row.status==='active')card.append(element('p','task-hint',`限定私信验收：${row.reply_test.account_uid} → ${row.reply_test.target_uid}；仅新私信，最多一次外发尝试。`));
      if(row.reason==='reply_test_complete')card.append(element('p','task-hint','本次限定私信验收已结束，请在任务记录核对发送结果。'));
      const options=element("div","platform-options"),durationLabel=element("label","","本次时长（分钟，留空不限）"),duration=element("input"),readLabel=element("label"),readOnly=element("input");
      duration.type="number";duration.min="1";duration.max="1440";duration.placeholder="不限";duration.value=saved.get(row.platform)?.duration||"";durationLabel.append(duration);
      readOnly.type="checkbox";readOnly.checked=saved.get(row.platform)?.readonly||false;readLabel.append(readOnly,document.createTextNode("只读运行"));options.append(durationLabel,readLabel);
      const actions=element("div","actions"),start=element("button"),stop=element("button","secondary");
      start.type=stop.type="button";start.dataset.runtimeStart="true";stop.dataset.runtimeStop="true";start.append(icon("play"),document.createTextNode("启动"));stop.append(icon("pause"),document.createTextNode("暂停"));
      const unavailable=!row.available||state.host?.available===false||state.host?.restart_required;
      start.disabled=unavailable||row.status==="active";stop.disabled=row.status!=="active";duration.disabled=readOnly.disabled=row.status==="active";
      for(const [button,action] of [[start,"start"],[stop,"stop"]])button.addEventListener("click",async()=>{
        if(action==="start"&&!duration.checkValidity()){duration.reportValidity();return;}
        button.disabled=true;try{
          const args=action==="start"?{read_only:readOnly.checked,...(row.platform==="local"?{background:false}:{}),...(duration.value?{duration_minutes:Number(duration.value)}:{})}:{};
          await operate("platform_control",{platform:row.platform,action,expected_generation:row.generation??null,options:args});
          platformSignature="";notice(names[row.platform]+(action==="start"?"已启动。":"已暂停。"));await refresh();
        }catch(e){notice(e.message);button.disabled=false;}
      });
      actions.append(start,stop);card.append(options,actions);grid.append(card);
    }
  }
  function activity(state){
    const list=[{platform:"local",...state},...(state.platforms||[])],active=list.filter(x=>x.status==="active"),offline=active.filter(disconnected),busy=active.filter(x=>x.busy&&!disconnected(x));
    text("#metric-platforms",active.length+" / 4");text("#metric-replies",format(state.quotas?.local?.remaining));
    text("#metric-replies-note","已用 "+format(state.quotas?.local?.used)+" / 上限 "+format(state.quotas?.local?.limit));
    const box=$("#activity-summary");if(!box)return;
    if(busy.length){box.replaceChildren(icon("spark"),element("h3","",busy.map(x=>names[x.platform]).join("、")+"正在处理任务"),element("p","","执行状态会自动更新，关闭此页面不会中断任务。"));}
    else if(offline.length){box.replaceChildren(icon("moon"),element("h3","","部分平台等待恢复连接"),element("p","",offline.map(x=>names[x.platform]).join("、")+"尚未连接，恢复后继续接收消息。"));}
    else if(active.length){box.replaceChildren(icon("cup"),element("h3","","陪伴已就绪"),element("p","",active.map(x=>names[x.platform]).join("、")+"正在等待下一次交流。"));}
    else empty(box,"休息一下，也很好",state.host?.restart_required?"请从管理聊天或命令行重启服务。":"所有平台已暂停。准备好了，再开启陪伴。");
  }
  window.renderQibanState=state=>{hostBanner(state);if(admin){runtimeCards(state);activity(state);}};
  function quotaTile(label,used,limit,remaining,note){
    const tile=element("article","quota-tile"),values=element("div","quota-value");tile.append(element("h3","",label));values.append(element("strong","",format(remaining)),element("span","","剩余 / 上限 "+format(limit)));tile.append(values);
    if(typeof used==="number"&&typeof limit==="number"){const progress=element("progress");progress.max=Math.max(1,limit);progress.value=Math.min(progress.max,used);progress.setAttribute("aria-label",label+"已用 "+used+" / "+limit);tile.append(progress);}
    tile.append(element("p","",note||"已用 "+format(used)+" · 按设定时区每日计数"));return tile;
  }
  function renderReport(data){
    reportData=data;const quotas=$("#usage-quotas");quotas.replaceChildren(...data.quotas.map(q=>quotaTile(q.label,q.used,q.limit,q.remaining)));
    text("#quota-timezone",data.timezone+" · 每日计数");
    const plan=data.coding_plan||{},windows=$("#plan-windows");windows.replaceChildren();
    text("#plan-freshness",plan.status!=="available"?"暂不可用"+(plan.updated_at?" · 保留上次快照":""):plan.stale?"数据已过期":"同步于 "+time(plan.updated_at));
    for(const bucket of plan.buckets||[]){for(const win of bucket.windows||[]){
      const hours=win.window_minutes===null?"未知周期":win.window_minutes>=1440?format(win.window_minutes/1440)+" 天":win.window_minutes>=60?format(win.window_minutes/60)+" 小时":format(win.window_minutes)+" 分钟";
      const tile=quotaTile(bucket.name+" · "+hours,win.used_percent,100,win.remaining_percent,"已用 "+format(win.used_percent)+"% · 重置于 "+time(win.resets_at));
      const strong=tile.querySelector("strong");strong.textContent=win.remaining_percent===null?"未知":format(win.remaining_percent)+"%";windows.append(tile);
    }}
    if(!windows.children.length)empty(windows,"暂时没有额度数据","请确认 Codex 登录与宿主连接；无法读取时不将余额视为零。");
    const bucket=plan.buckets?.find(b=>b.id==="codex")||plan.buckets?.[0],primary=bucket?.windows?.[0];
    text("#metric-plan",primary?.remaining_percent!=null?format(primary.remaining_percent)+"%":"—");
    text("#metric-plan-note",plan.status!=="available"||plan.stale?"额度快照暂不可用或已过期":"账户共享 · 已用 "+format(primary?.used_percent)+"%");
    const tokens=data.tokens||{},counters=$("#token-counters");counters.replaceChildren();
    for(const [key,label] of [["input_tokens","输入 tokens"],["cached_input_tokens","其中：缓存输入"],["output_tokens","输出 tokens"],["reasoning_output_tokens","其中：推理输出"]]){
      const metric=element("article","metric");metric.append(element("span","",label),element("strong","",tokens[key]===null?"未知":format(tokens[key])),element("small","",format(data.token_known_calls[key])+" 次调用已上报"));counters.append(metric);
    }
    text("#metric-tokens",typeof tokens.input_tokens==="number"&&typeof tokens.output_tokens==="number"?format(tokens.input_tokens+tokens.output_tokens):"—");
    text("#metric-tokens-note","最近 "+data.days+" 天 · Qiban");
    text("#token-coverage","最近 "+data.days+" 天共记录 "+data.model_calls+" 次模型调用。"+(data.write_failures?"本次服务有 "+data.write_failures+" 次观测写入失败，统计可能不完整。":""));
    const rows=$("#observation-rows");rows.replaceChildren();for(const item of data.recent){const tr=element("tr");for(const value of [new Date(item.started).toLocaleString("zh-CN",{hour12:false}),item.stage,item.status==="running"?"未记录结束":phases[item.status]||item.status,item.duration_ms===null?"—":format(item.duration_ms)+" ms",format(item.input_tokens)+" / "+format(item.output_tokens)])tr.append(element("td","",value));rows.append(tr);}
    const tasks=$("#runtime-tasks");tasks.replaceChildren();
    if(!data.tasks.length)empty(tasks,"暂时没有任务手记","在会客室提交任务后，这里会显示它的执行进度。");
    for(const task of data.tasks){const row=element("div","runtime-task"),description=element("div");description.append(element("strong","",(phases[task.status]||task.status)+" · "+(phases[task.phase]||task.phase||"等待")),element("p","",task.id+(task.error?" · "+task.error:"")));row.append(description);
      if(["queued","running"].includes(task.status)){const cancel=element("button","quiet","取消任务");cancel.type="button";cancel.addEventListener("click",async()=>{cancel.disabled=true;try{await operate("task_cancel",{id:task.id,expected_revision:task.revision});await refreshMonitor();notice("任务已取消。");}catch(e){notice(e.message);cancel.disabled=false;}});row.append(cancel);}tasks.append(row);
    }
  }
  async function refreshMonitor(){
    if(!admin||loading)return;loading=true;
    try{renderReport(await operate("runtime_monitor",{days:Number($("#monitor-days").value)}));}
    catch(error){notice("运行监控暂未更新："+error.message);if(!reportData){text("#plan-freshness","暂不可用");}}
    finally{loading=false;}
  }
  async function replyUsage(){
    try{const data=await operate("reply_policy_list"),container=$("#reply-usage");container.replaceChildren();
      for(const item of data.items){const used=item.private_usage.confirmed+item.private_usage.reserved+item.private_usage.uncertain,limit=item.settings.private_daily_replies;container.append(quotaTile(names[item.platform]+" · "+item.account_label+" · 私聊",used,limit,Math.max(0,limit-used),"成功 "+item.private_usage.confirmed+" · 处理中 "+item.private_usage.reserved+" · 结果不确定 "+item.private_usage.uncertain));}
      for(const group of data.groups||[]){container.append(quotaTile(names[group.platform]+" · "+group.label+" · 群聊",group.used,group.reply_limit,group.active?Math.max(0,group.reply_limit-group.used):null,(group.active?"本段参与":"上一段参与 / 尚未唤起")+" · 已用回复 "+format(group.used)+" · 已决策 "+format(group.decisions)+"；设定每段回复 "+format(group.settings.group_session_replies)+" / 决策 "+format(group.settings.group_session_decisions)+" 次 / "+format(group.settings.group_session_minutes)+" 分钟"));}
    }catch(error){text("#reply-usage","平台私聊用量暂不可用："+error.message);}
  }
  if(admin){
    let testStarting=false;
    bind('#bili-test-form',async()=>{
      if(testStarting)return;
      const form=$('#bili-test-form');if(!form.reportValidity())return;
      testStarting=true;$('#bili-test-start').disabled=true;
      try{
        const state=await api('/status'),row=state.platforms?.find(p=>p.platform==='bilibili');
        if(!row?.available)throw new Error('B站连接器尚未就绪');
        if(row.status==='active')throw new Error('请先暂停B站，再启动限定私信验收');
        await operate('platform_control',{platform:'bilibili',action:'start',expected_generation:row.generation??null,
          options:{duration_minutes:Number($('#bili-test-duration').value),reply_test:{
            account_uid:$('#bili-test-account').value.trim(),target_uid:$('#bili-test-target').value.trim()}}});
        notice('限定私信验收已启动：请由指定对象发送新的编号消息。');await refresh();
      }finally{testStarting=false;$('#bili-test-start').disabled=false;}
    },'submit');
    bind("#open-codex",async()=>{await operate("open_codex",{});notice("已请求打开 Codex。请在管理聊天选择 @qiban，明确发出所需指令。");});
    bind("#copy-qiban",async()=>{await navigator.clipboard.writeText("$qiban 打开栖伴管理页，查看运行状态和额度。");notice("管理指令已复制，请在 Codex 聊天中粘贴。");});
    bind("#monitor-refresh",async()=>{await refreshMonitor();await replyUsage();});bind("#tasks-refresh",refreshMonitor);bind("#monitor-days",refreshMonitor,"change");
    refreshMonitor();replyUsage();let round=0;const poll=async()=>{if(!document.hidden){await refreshMonitor();if(++round%4===0)await replyUsage();}setTimeout(poll,15000);};setTimeout(poll,15000);
  }
})();
